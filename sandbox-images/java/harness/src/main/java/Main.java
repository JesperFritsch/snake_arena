import com.google.protobuf.ByteString;
import io.grpc.ManagedChannel;
import io.grpc.ManagedChannelBuilder;
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
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

public class Main extends RemoteSnakeGrpc.RemoteSnakeImplBase {

    private final SnakeInterface snake;
    // False only for the warm-up instance, so it doesn't emit step markers
    // that would shift the dev console's per-step logs.
    private final boolean emitStepMarkers;
    private int height, width;
    private String dtype = "uint8";

    public Main(SnakeInterface snake, boolean emitStepMarkers) {
        this.snake = snake;
        this.emitStepMarkers = emitStepMarkers;
    }

    private static int[][] bytesToGrid(byte[] data, int h, int w, String dt) {
        int n = h * w;
        int[] flat = new int[n];
        ByteBuffer buf = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
        switch (dt) {
            case "int8": case "|i1":
                for (int i = 0; i < n && i < data.length; i++)
                    flat[i] = data[i];
                break;
            case "uint8": case "|u1":
                for (int i = 0; i < n && i < data.length; i++)
                    flat[i] = data[i] & 0xFF;
                break;
            case "int16": case "<i2":
                for (int i = 0; i < n && (i + 1) * 2 <= data.length; i++)
                    flat[i] = buf.getShort(i * 2);
                break;
            case "uint16": case "<u2":
                for (int i = 0; i < n && (i + 1) * 2 <= data.length; i++)
                    flat[i] = buf.getShort(i * 2) & 0xFFFF;
                break;
            case "int32": case "<i4":
            case "uint32": case "<u4":
                for (int i = 0; i < n && (i + 1) * 4 <= data.length; i++)
                    flat[i] = buf.getInt(i * 4);
                break;
            case "int64": case "<i8":
            case "uint64": case "<u8":
                for (int i = 0; i < n && (i + 1) * 8 <= data.length; i++)
                    flat[i] = (int) buf.getLong(i * 8);
                break;
            case "float32": case "<f4":
                for (int i = 0; i < n && (i + 1) * 4 <= data.length; i++)
                    flat[i] = (int) buf.getFloat(i * 4);
                break;
            case "float64": case "<f8":
                for (int i = 0; i < n && (i + 1) * 8 <= data.length; i++)
                    flat[i] = (int) buf.getDouble(i * 8);
                break;
            default:
                throw new IllegalArgumentException("unsupported dtype: " + dt);
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
                if (emitStepMarkers) {
                    System.out.println("---STEP_END---");
                    System.out.flush();
                }
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

    // The runner only charges startup CPU from the moment port 50051 accepts
    // gRPC connections until the sim finishes its init calls. A cold JVM
    // spends hundreds of ms of CPU class-loading netty/gRPC/protobuf on the
    // first calls, which would blow that budget. So before binding the real
    // port, drive the whole RPC sequence the sim uses through a throwaway
    // loopback server with a no-op snake. User code is never called here.
    private static void warmUp() {
        Server server = null;
        ManagedChannel channel = null;
        try {
            server = ServerBuilder.forPort(0)
                .addService(new Main(new NoopSnake(), false)).build().start();
            channel = ManagedChannelBuilder.forAddress("127.0.0.1", server.getPort())
                .usePlaintext().build();
            RemoteSnakeGrpc.RemoteSnakeBlockingStub blocking = RemoteSnakeGrpc.newBlockingStub(channel);

            int h = 8, w = 8;
            ByteString grid = ByteString.copyFrom(new byte[h * w]);
            SnakeSim.Coord pos = SnakeSim.Coord.newBuilder().setX(1).setY(1).build();

            blocking.setId(SnakeSim.SnakeId.newBuilder().setId(0).build());
            blocking.setStartLength(SnakeSim.StartLength.newBuilder().setLength(1).build());
            blocking.setStartPosition(SnakeSim.StartPosition.newBuilder().setStartPosition(pos).build());
            blocking.setInitData(SnakeSim.EnvInitData.newBuilder()
                .setHeight(h).setWidth(w)
                .setFreeValue(0).setBlockedValue(1).setFoodValue(2)
                .putSnakeTags(0, "warmup")
                .putSnakeValues(0, SnakeSim.SnakeValues.newBuilder().setHeadValue(3).setBodyValue(4).build())
                .putStartPositions(0, pos)
                .setBaseMap(grid).setBaseMapDtype("uint8")
                .build());

            int steps = 20;
            CountDownLatch done = new CountDownLatch(steps);
            StreamObserver<SnakeSim.EnvData> updates = RemoteSnakeGrpc.newStub(channel).update(
                new StreamObserver<>() {
                    @Override public void onNext(SnakeSim.UpdateResponse r) { done.countDown(); }
                    @Override public void onError(Throwable t) {
                        while (done.getCount() > 0) done.countDown();
                    }
                    @Override public void onCompleted() {}
                });
            SnakeSim.EnvData envData = SnakeSim.EnvData.newBuilder()
                .setMap(grid)
                .putSnakes(0, SnakeSim.SnakeRep.newBuilder().setIsAlive(true).setLength(1).build())
                .addFoodLocations(pos)
                .build();
            for (int i = 0; i < steps; i++) updates.onNext(envData);
            done.await(5, TimeUnit.SECONDS);
            updates.onCompleted();

            blocking.reset(SnakeSim.Empty.getDefaultInstance());
            blocking.kill(SnakeSim.Empty.getDefaultInstance());
        } catch (Throwable t) {
            // Warm-up is best effort; the real server must start regardless.
            System.err.println("harness warm-up failed: " + t);
        } finally {
            if (channel != null) {
                channel.shutdownNow();
                try { channel.awaitTermination(2, TimeUnit.SECONDS); } catch (InterruptedException ignored) {}
            }
            if (server != null) {
                server.shutdownNow();
                try { server.awaitTermination(2, TimeUnit.SECONDS); } catch (InterruptedException ignored) {}
            }
        }
    }

    private static final class NoopSnake implements SnakeInterface {
        @Override public void setId(int id) {}
        @Override public void setStartLength(int n) {}
        @Override public void setStartPosition(Coord pos) {}
        @Override public void setInitData(EnvInitData data) {}
        @Override public int[] update(EnvStepData data) { return new int[]{ 0, 1 }; }
    }

    public static void main(String[] args) throws IOException, InterruptedException {
        warmUp();
        Server server = ServerBuilder.forPort(50051).addService(new Main(new Snake(), true)).build().start();
        server.awaitTermination();
    }
}
