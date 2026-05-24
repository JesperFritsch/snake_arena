process.env.GRPC_VERBOSITY = 'ERROR';

import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import * as path from 'path';
import { Snake } from './usercode/snake';
import type { Coord, EnvInitData, EnvStepData } from './types';

const packageDef = protoLoader.loadSync(
  path.join(__dirname, '..', '..', 'proto', 'sim_interface.proto'),
  { keepCase: true, longs: Number, enums: String, defaults: true, oneofs: true },
);
const { snake_sim } = grpc.loadPackageDefinition(packageDef) as any;

const snake = new Snake();
let height = 0, width = 0, dtype = 'int32';

function bytesToGrid(raw: Buffer | Uint8Array, h: number, w: number, dt: string): number[][] {
  const buf: Buffer = raw instanceof Buffer ? raw : Buffer.from(raw);
  const n = h * w;
  const flat: number[] = new Array(n).fill(0);
  if (dt === 'int64' || dt === '<i8' || dt === '>i8') {
    for (let i = 0; i < n && (i + 1) * 8 <= buf.length; i++)
      flat[i] = Number(buf.readBigInt64LE(i * 8));
  } else if (dt === 'float32' || dt === '<f4' || dt === '>f4') {
    for (let i = 0; i < n && (i + 1) * 4 <= buf.length; i++)
      flat[i] = buf.readFloatLE(i * 4);
  } else {
    for (let i = 0; i < n && (i + 1) * 4 <= buf.length; i++)
      flat[i] = buf.readInt32LE(i * 4);
  }
  const grid: number[][] = [];
  for (let r = 0; r < h; r++)
    grid.push(flat.slice(r * w, (r + 1) * w));
  return grid;
}

const service = {
  SetId(call: any, cb: any) {
    snake.setId(call.request.id);
    cb(null, {});
  },
  SetStartLength(call: any, cb: any) {
    snake.setStartLength(call.request.length);
    cb(null, {});
  },
  SetStartPosition(call: any, cb: any) {
    const c = call.request.start_position ?? { x: 0, y: 0 };
    snake.setStartPosition({ x: c.x, y: c.y } as Coord);
    cb(null, {});
  },
  SetInitData(call: any, cb: any) {
    const req = call.request;
    height = req.height;
    width  = req.width;
    dtype  = req.base_map_dtype;
    const data: EnvInitData = {
      height: req.height,
      width:  req.width,
      free_value:    req.free_value,
      blocked_value: req.blocked_value,
      food_value:    req.food_value,
      snake_tags:      Object.fromEntries(Object.entries(req.snake_tags).map(([k, v]) => [Number(k), v as string])),
      snake_values:    Object.fromEntries(Object.entries(req.snake_values).map(([k, v]: [string, any]) => [Number(k), { head_value: v.head_value, body_value: v.body_value }])),
      start_positions: Object.fromEntries(Object.entries(req.start_positions).map(([k, c]: [string, any]) => [Number(k), { x: c.x, y: c.y }])),
      base_map: bytesToGrid(req.base_map, req.height, req.width, req.base_map_dtype),
      base_map_dtype: req.base_map_dtype,
    };
    snake.setInitData(data);
    cb(null, {});
  },
  Update(call: any) {
    call.on('data', (envData: any) => {
      const stepData: EnvStepData = {
        map: bytesToGrid(envData.map, height, width, dtype),
        snakes: Object.fromEntries(
          Object.entries(envData.snakes).map(([k, s]: [string, any]) => [Number(k), { is_alive: s.is_alive, length: s.length }])
        ),
        food_locations: (envData.food_locations ?? []).map((c: any) => ({ x: c.x, y: c.y })),
      };
      try {
        const dir = snake.update(stepData);
        process.stdout.write('---STEP_END---\n');
        call.write({ direction: { x: dir[0], y: dir[1] } });
      } catch (err: any) {
        process.stdout.write(String(err?.stack ?? err) + '\n---STEP_END---\n');
        call.destroy(err instanceof Error ? err : new Error(String(err)));
      }
    });
    call.on('end', () => call.end());
  },
  Reset(_call: any, cb: any) { cb(null, {}); },
  Kill(_call: any, cb: any)  { cb(null, {}); },
};

const server = new grpc.Server();
server.addService(snake_sim.RemoteSnake.service, service);
server.bindAsync('0.0.0.0:50051', grpc.ServerCredentials.createInsecure(), (err: Error | null) => {
  if (err) { console.error(err); process.exit(1); }
});
