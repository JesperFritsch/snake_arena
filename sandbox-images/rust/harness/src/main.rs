use std::io::Write;
use std::sync::Arc;
use tokio::sync::{mpsc, Mutex};
use tokio_stream::{wrappers::ReceiverStream, StreamExt};
use tonic::{transport::Server, Request, Response, Status, Streaming};

mod snake;
mod types;

pub mod snake_sim {
    tonic::include_proto!("snake_sim");
}

use snake_sim::{
    remote_snake_server::{RemoteSnake, RemoteSnakeServer},
    Coord as ProtoCoord, Empty, EnvData, EnvInitData as ProtoEnvInitData, SnakeId, StartLength,
    StartPosition, UpdateResponse,
};

struct SnakeState {
    snake: snake::Snake,
    height: i32,
    width: i32,
    dtype: String,
}

struct SnakeService {
    state: Arc<Mutex<SnakeState>>,
}

impl SnakeService {
    fn new() -> Self {
        Self {
            state: Arc::new(Mutex::new(SnakeState {
                snake: snake::Snake::new(),
                height: 0,
                width: 0,
                dtype: String::new(),
            })),
        }
    }
}

fn bytes_to_grid(data: &[u8], height: usize, width: usize, dtype: &str) -> Vec<Vec<i32>> {
    let n = height * width;
    let mut flat = Vec::with_capacity(n);
    match dtype {
        "int32" | "<i4" | ">i4" => {
            for chunk in data.chunks_exact(4) {
                flat.push(i32::from_le_bytes(chunk.try_into().unwrap_or([0; 4])));
            }
        }
        "int64" | "<i8" | ">i8" => {
            for chunk in data.chunks_exact(8) {
                flat.push(i64::from_le_bytes(chunk.try_into().unwrap_or([0; 8])) as i32);
            }
        }
        "float32" | "<f4" | ">f4" => {
            for chunk in data.chunks_exact(4) {
                flat.push(f32::from_le_bytes(chunk.try_into().unwrap_or([0; 4])) as i32);
            }
        }
        _ => {
            for chunk in data.chunks_exact(4) {
                flat.push(i32::from_le_bytes(chunk.try_into().unwrap_or([0; 4])));
            }
        }
    }
    flat.resize(n, 0);
    flat.chunks(width).map(|row| row.to_vec()).collect()
}

fn proto_init_to_types(msg: ProtoEnvInitData) -> types::EnvInitData {
    let h = msg.height as usize;
    let w = msg.width as usize;
    let dtype = msg.base_map_dtype.clone();
    let base_map = bytes_to_grid(&msg.base_map, h, w, &dtype);
    types::EnvInitData {
        height: msg.height,
        width: msg.width,
        free_value: msg.free_value,
        blocked_value: msg.blocked_value,
        food_value: msg.food_value,
        snake_tags: msg.snake_tags,
        snake_values: msg
            .snake_values
            .into_iter()
            .map(|(k, v)| {
                (
                    k,
                    types::SnakeValues {
                        head_value: v.head_value,
                        body_value: v.body_value,
                    },
                )
            })
            .collect(),
        start_positions: msg
            .start_positions
            .into_iter()
            .map(|(k, c)| (k, types::Coord { x: c.x, y: c.y }))
            .collect(),
        base_map,
        base_map_dtype: dtype,
    }
}

fn proto_env_to_types(
    msg: EnvData,
    height: i32,
    width: i32,
    dtype: &str,
) -> types::EnvStepData {
    let map = bytes_to_grid(&msg.map, height as usize, width as usize, dtype);
    types::EnvStepData {
        map,
        snakes: msg
            .snakes
            .into_iter()
            .map(|(k, s)| {
                (
                    k,
                    types::SnakeRep {
                        is_alive: s.is_alive,
                        length: s.length,
                    },
                )
            })
            .collect(),
        food_locations: msg
            .food_locations
            .into_iter()
            .map(|c| types::Coord { x: c.x, y: c.y })
            .collect(),
    }
}

#[tonic::async_trait]
impl RemoteSnake for SnakeService {
    type UpdateStream = ReceiverStream<Result<UpdateResponse, Status>>;

    async fn set_id(&self, request: Request<SnakeId>) -> Result<Response<Empty>, Status> {
        self.state
            .lock()
            .await
            .snake
            .set_id(request.into_inner().id);
        Ok(Response::new(Empty {}))
    }

    async fn set_start_length(
        &self,
        request: Request<StartLength>,
    ) -> Result<Response<Empty>, Status> {
        self.state
            .lock()
            .await
            .snake
            .set_start_length(request.into_inner().length);
        Ok(Response::new(Empty {}))
    }

    async fn set_start_position(
        &self,
        request: Request<StartPosition>,
    ) -> Result<Response<Empty>, Status> {
        let coord = request.into_inner().start_position.unwrap_or_default();
        self.state
            .lock()
            .await
            .snake
            .set_start_position(types::Coord { x: coord.x, y: coord.y });
        Ok(Response::new(Empty {}))
    }

    async fn set_init_data(
        &self,
        request: Request<ProtoEnvInitData>,
    ) -> Result<Response<Empty>, Status> {
        let msg = request.into_inner();
        let height = msg.height;
        let width = msg.width;
        let dtype = msg.base_map_dtype.clone();
        let init_data = proto_init_to_types(msg);
        let mut state = self.state.lock().await;
        state.height = height;
        state.width = width;
        state.dtype = dtype;
        state.snake.set_init_data(init_data);
        Ok(Response::new(Empty {}))
    }

    async fn update(
        &self,
        request: Request<Streaming<EnvData>>,
    ) -> Result<Response<Self::UpdateStream>, Status> {
        let (tx, rx) = mpsc::channel(128);
        let state = self.state.clone();

        tokio::spawn(async move {
            let mut stream = request.into_inner();
            while let Some(Ok(msg)) = stream.next().await {
                let direction = {
                    let mut s = state.lock().await;
                    let h = s.height;
                    let w = s.width;
                    let dtype = s.dtype.clone();
                    let step_data = proto_env_to_types(msg, h, w, &dtype);
                    s.snake.update(step_data)
                };

                print!("---STEP_END---\n");
                let _ = std::io::stdout().flush();

                let (dx, dy) = direction;
                let response = UpdateResponse {
                    direction: Some(ProtoCoord { x: dx, y: dy }),
                };
                if tx.send(Ok(response)).await.is_err() {
                    break;
                }
            }
        });

        Ok(Response::new(ReceiverStream::new(rx)))
    }

    async fn reset(&self, _request: Request<Empty>) -> Result<Response<Empty>, Status> {
        Ok(Response::new(Empty {}))
    }

    async fn kill(&self, _request: Request<Empty>) -> Result<Response<Empty>, Status> {
        Ok(Response::new(Empty {}))
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let addr = "0.0.0.0:50051".parse()?;
    Server::builder()
        .add_service(RemoteSnakeServer::new(SnakeService::new()))
        .serve(addr)
        .await?;
    Ok(())
}
