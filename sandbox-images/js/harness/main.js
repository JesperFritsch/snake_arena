'use strict';

process.env.GRPC_VERBOSITY = 'ERROR';

const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');
const path = require('path');

const packageDef = protoLoader.loadSync(
  path.join(__dirname, '..', 'proto', 'sim_interface.proto'),
  { keepCase: true, longs: Number, enums: String, defaults: true, oneofs: true },
);
const { snake_sim } = grpc.loadPackageDefinition(packageDef);

const { Snake } = require('./usercode/snake');
const snake = new Snake();
let height = 0, width = 0, dtype = 'int32';

function bytesToGrid(buf, h, w, dt) {
  if (!(buf instanceof Buffer)) buf = Buffer.from(buf);
  const n = h * w;
  const flat = new Array(n).fill(0);
  if (dt === 'int8' || dt === '|i1') {
    for (let i = 0; i < n && i < buf.length; i++)
      flat[i] = buf.readInt8(i);
  } else if (dt === 'uint8' || dt === '|u1') {
    for (let i = 0; i < n && i < buf.length; i++)
      flat[i] = buf[i];
  } else if (dt === 'int16' || dt === '<i2') {
    for (let i = 0; i < n && (i + 1) * 2 <= buf.length; i++)
      flat[i] = buf.readInt16LE(i * 2);
  } else if (dt === 'uint16' || dt === '<u2') {
    for (let i = 0; i < n && (i + 1) * 2 <= buf.length; i++)
      flat[i] = buf.readUInt16LE(i * 2);
  } else if (dt === 'int32' || dt === '<i4') {
    for (let i = 0; i < n && (i + 1) * 4 <= buf.length; i++)
      flat[i] = buf.readInt32LE(i * 4);
  } else if (dt === 'uint32' || dt === '<u4') {
    for (let i = 0; i < n && (i + 1) * 4 <= buf.length; i++)
      flat[i] = buf.readUInt32LE(i * 4);
  } else if (dt === 'int64' || dt === '<i8') {
    for (let i = 0; i < n && (i + 1) * 8 <= buf.length; i++)
      flat[i] = Number(buf.readBigInt64LE(i * 8));
  } else if (dt === 'uint64' || dt === '<u8') {
    for (let i = 0; i < n && (i + 1) * 8 <= buf.length; i++)
      flat[i] = Number(buf.readBigUInt64LE(i * 8));
  } else if (dt === 'float32' || dt === '<f4') {
    for (let i = 0; i < n && (i + 1) * 4 <= buf.length; i++)
      flat[i] = buf.readFloatLE(i * 4);
  } else if (dt === 'float64' || dt === '<f8') {
    for (let i = 0; i < n && (i + 1) * 8 <= buf.length; i++)
      flat[i] = buf.readDoubleLE(i * 8);
  } else {
    throw new Error(`unsupported dtype: ${dt}`);
  }
  const grid = [];
  for (let r = 0; r < h; r++)
    grid.push(flat.slice(r * w, (r + 1) * w));
  return grid;
}

const service = {
  SetId(call, cb) {
    snake.setId(call.request.id);
    cb(null, {});
  },
  SetStartLength(call, cb) {
    snake.setStartLength(call.request.length);
    cb(null, {});
  },
  SetStartPosition(call, cb) {
    const c = call.request.start_position ?? { x: 0, y: 0 };
    snake.setStartPosition({ x: c.x, y: c.y });
    cb(null, {});
  },
  SetInitData(call, cb) {
    const req = call.request;
    height = req.height;
    width  = req.width;
    dtype  = req.base_map_dtype;
    snake.setInitData({
      height: req.height,
      width:  req.width,
      free_value:    req.free_value,
      blocked_value: req.blocked_value,
      food_value:    req.food_value,
      // map keys are strings (proto int32 map keys serialise as strings in JS)
      snake_tags:       Object.fromEntries(Object.entries(req.snake_tags).map(([k,v])=>[Number(k),v])),
      snake_values:     Object.fromEntries(Object.entries(req.snake_values).map(([k,v])=>[Number(k),{head_value:v.head_value,body_value:v.body_value}])),
      start_positions:  Object.fromEntries(Object.entries(req.start_positions).map(([k,c])=>[Number(k),{x:c.x,y:c.y}])),
      base_map: bytesToGrid(req.base_map, req.height, req.width, req.base_map_dtype),
      base_map_dtype: req.base_map_dtype,
    });
    cb(null, {});
  },
  Update(call) {
    call.on('data', (envData) => {
      const stepData = {
        map: bytesToGrid(envData.map, height, width, dtype),
        snakes: Object.fromEntries(
          Object.entries(envData.snakes).map(([k,s]) => [Number(k), { is_alive: s.is_alive, length: s.length }])
        ),
        food_locations: (envData.food_locations ?? []).map(c => ({ x: c.x, y: c.y })),
      };
      try {
        const dir = snake.update(stepData);
        process.stdout.write('---STEP_END---\n');
        call.write({ direction: { x: dir[0], y: dir[1] } });
      } catch (err) {
        process.stdout.write(String(err?.stack ?? err) + '\n---STEP_END---\n');
        call.destroy(err instanceof Error ? err : new Error(String(err)));
      }
    });
    call.on('end', () => call.end());
  },
  Reset(call, cb) { cb(null, {}); },
  Kill(call, cb)  { cb(null, {}); },
};

const server = new grpc.Server();
server.addService(snake_sim.RemoteSnake.service, service);
server.bindAsync('0.0.0.0:50051', grpc.ServerCredentials.createInsecure(), (err) => {
  if (err) { console.error(err); process.exit(1); }
});
