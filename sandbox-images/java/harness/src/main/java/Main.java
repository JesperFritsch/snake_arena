import io.grpc.Server;
import io.grpc.ServerBuilder;
import io.grpc.stub.StreamObserver;
import snake_sim.RemoteSnakeGrpc;
import snake_sim.SnakeSim;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class Main extends RemoteSnakeGrpc.RemoteSnakeImplBase {

    private final SnakeInterface snake = new Snake();
    private int height, width;
    private String dtype = "int32";

    private static int[][] bytesToGrid(byte[] data, int h, int w, String dt) {
        int n = h * w;
        int[] flat = new int[n];
        ByteBuffer buf = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
        switch (dt) {
            case "int64": case "<i8": case ">i8":
                for (int i = 0; i < n && (i + 1) * 8 <= data.length; i++)
                    flat[i] = (int) buf.getLong(i * 8);
                break;
            case "float32": case "<f4": case ">f4":
                for (int i = 0; i < n && (i + 1) * 4 <= data.length; i++)
                    flat[i] = (int) buf.getFloat(i * 4);
                break;
            default:
                for (int i = 0; i < n && (i + 1) * 4 <= data.length; i++)
                    flat[i] = buf.getInt(i * 4);
        }
        int[][] grid = new int[h][w];
        for (int r = 0; r < h; r++)
            System.arraycopy(flat, r * w, grid[r], 0, w);
        return grid;
    }

    @Override
    public void setId(SnakeSim.SnakeId req, StreamObserver<SnakeSim.Empty> resp) {
        snake.setId(req.getId());
        resp.onNext(SnakeSim.Empty.getDefaultInstance());
        resp.onCompleted();
    }

    @Override
    public void setStartLength(SnakeSim.StartLength req, StreamObserver<SnakeSim.Empty> resp) {
        snake.setStartLength(req.getLength());
        resp.onNext(SnakeSim.Empty.getDefaultInstance());
        resp.onCompleted();
    }

    @Override
    public void setStartPosition(SnakeSim.StartPosition req, StreamObserver<SnakeSim.Empty> resp) {
        SnakeSim.Coord c = req.hasStartPosition() ? req.getStartPosition() : SnakeSim.Coord.getDefaultInstance();
        snake.setStartPosition(new Coord(c.getX(), c.getY()));
        resp.onNext(SnakeSim.Empty.getDefaultInstance());
        resp.onCompleted();
    }

    @Override
    public void setInitData(SnakeSim.EnvInitData req, StreamObserver<SnakeSim.Empty> resp) {
        height = req.getHeight();
        width = req.getWidth();
        dtype = req.getBaseMapDtype();

        Map<Integer, String> snakeTags = new HashMap<>();
        for (Map.Entry<Integer, String> e : req.getSnakeTagsMap().entrySet())
            snakeTags.put(e.getKey(), e.getValue());

        Map<Integer, SnakeValues> snakeValues = new HashMap<>();
        for (Map.Entry<Integer, SnakeSim.SnakeValues> e : req.getSnakeValuesMap().entrySet())
            snakeValues.put(e.getKey(), new SnakeValues(e.getValue().getHeadValue(), e.getValue().getBodyValue()));

        Map<Integer, Coord> startPositions = new HashMap<>();
        for (Map.Entry<Integer, SnakeSim.Coord> e : req.getStartPositionsMap().entrySet())
            startPositions.put(e.getKey(), new Coord(e.getValue().getX(), e.getValue().getY()));

        int[][] baseMap = bytesToGrid(req.getBaseMap().toByteArray(), height, width, dtype);

        snake.setInitData(new EnvInitData(
            height, width,
            req.getFreeValue(), req.getBlockedValue(), req.getFoodValue(),
            snakeTags, snakeValues, startPositions,
            baseMap, dtype
        ));
        resp.onNext(SnakeSim.Empty.getDefaultInstance());
        resp.onCompleted();
    }

    @Override
    public StreamObserver<SnakeSim.EnvData> update(StreamObserver<SnakeSim.UpdateResponse> resp) {
        return new StreamObserver<>() {
            @Override
            public void onNext(SnakeSim.EnvData envData) {
                int[][] map = bytesToGrid(envData.getMap().toByteArray(), height, width, dtype);

                Map<Integer, SnakeRep> snakes = new HashMap<>();
                for (Map.Entry<Integer, SnakeSim.SnakeRep> e : envData.getSnakesMap().entrySet())
                    snakes.put(e.getKey(), new SnakeRep(e.getValue().getIsAlive(), e.getValue().getLength()));

                List<Coord> food = new ArrayList<>();
                for (SnakeSim.Coord c : envData.getFoodLocationsList())
                    food.add(new Coord(c.getX(), c.getY()));

                int[] dir = snake.update(new EnvStepData(map, snakes, food));
                System.out.println("---STEP_END---");
                System.out.flush();
                resp.onNext(SnakeSim.UpdateResponse.newBuilder()
                    .setDirection(SnakeSim.Coord.newBuilder().setX(dir[0]).setY(dir[1]))
                    .build());
            }

            @Override
            public void onError(Throwable t) { resp.onError(t); }

            @Override
            public void onCompleted() { resp.onCompleted(); }
        };
    }

    @Override
    public void reset(SnakeSim.Empty req, StreamObserver<SnakeSim.Empty> resp) {
        resp.onNext(SnakeSim.Empty.getDefaultInstance());
        resp.onCompleted();
    }

    @Override
    public void kill(SnakeSim.Empty req, StreamObserver<SnakeSim.Empty> resp) {
        resp.onNext(SnakeSim.Empty.getDefaultInstance());
        resp.onCompleted();
    }

    public static void main(String[] args) throws IOException, InterruptedException {
        Server server = ServerBuilder.forPort(50051).addService(new Main()).build().start();
        server.awaitTermination();
    }
}
