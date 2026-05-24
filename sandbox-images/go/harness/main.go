package main

import (
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"math"
	"net"
	"os"
	"sync"

	pb "harness/pb"

	"google.golang.org/grpc"
)

type snakeServer struct {
	pb.UnimplementedRemoteSnakeServer
	mu     sync.Mutex
	snake  *Snake
	height int32
	width  int32
	dtype  string
}

func newServer() *snakeServer {
	return &snakeServer{snake: NewSnake()}
}

func bytesToGrid(data []byte, height, width int32, dtype string) [][]int32 {
	n := int(height) * int(width)
	flat := make([]int32, n)
	switch dtype {
	case "int64", "<i8", ">i8":
		for i := 0; i < n && (i+1)*8 <= len(data); i++ {
			flat[i] = int32(int64(binary.LittleEndian.Uint64(data[i*8:])))
		}
	case "float32", "<f4", ">f4":
		for i := 0; i < n && (i+1)*4 <= len(data); i++ {
			flat[i] = int32(math.Float32frombits(binary.LittleEndian.Uint32(data[i*4:])))
		}
	default:
		for i := 0; i < n && (i+1)*4 <= len(data); i++ {
			flat[i] = int32(binary.LittleEndian.Uint32(data[i*4:]))
		}
	}
	grid := make([][]int32, height)
	for r := range grid {
		row := make([]int32, width)
		copy(row, flat[int(r)*int(width):(int(r)+1)*int(width)])
		grid[r] = row
	}
	return grid
}

func (s *snakeServer) SetId(_ context.Context, req *pb.SnakeId) (*pb.Empty, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	s.snake.SetId(req.Id)
	return &pb.Empty{}, nil
}

func (s *snakeServer) SetStartLength(_ context.Context, req *pb.StartLength) (*pb.Empty, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	s.snake.SetStartLength(req.Length)
	return &pb.Empty{}, nil
}

func (s *snakeServer) SetStartPosition(_ context.Context, req *pb.StartPosition) (*pb.Empty, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	c := req.StartPosition
	if c == nil {
		c = &pb.Coord{}
	}
	s.snake.SetStartPosition(Coord{X: c.X, Y: c.Y})
	return &pb.Empty{}, nil
}

func (s *snakeServer) SetInitData(_ context.Context, req *pb.EnvInitData) (*pb.Empty, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	s.height = req.Height
	s.width = req.Width
	s.dtype = req.BaseMapDtype
	init := EnvInitData{
		Height: req.Height, Width: req.Width,
		FreeValue: req.FreeValue, BlockedValue: req.BlockedValue, FoodValue: req.FoodValue,
		BaseMapDtype:   req.BaseMapDtype,
		SnakeTags:      make(map[int32]string, len(req.SnakeTags)),
		SnakeValues:    make(map[int32]SnakeValues, len(req.SnakeValues)),
		StartPositions: make(map[int32]Coord, len(req.StartPositions)),
	}
	for k, v := range req.SnakeTags {
		init.SnakeTags[k] = v
	}
	for k, v := range req.SnakeValues {
		init.SnakeValues[k] = SnakeValues{HeadValue: v.HeadValue, BodyValue: v.BodyValue}
	}
	for k, c := range req.StartPositions {
		init.StartPositions[k] = Coord{X: c.X, Y: c.Y}
	}
	init.BaseMap = bytesToGrid(req.BaseMap, req.Height, req.Width, req.BaseMapDtype)
	s.snake.SetInitData(init)
	return &pb.Empty{}, nil
}

func (s *snakeServer) Update(stream pb.RemoteSnake_UpdateServer) error {
	for {
		req, err := stream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		var dx, dy int32
		func() {
			s.mu.Lock(); defer s.mu.Unlock()
			step := EnvStepData{
				Map:           bytesToGrid(req.Map, s.height, s.width, s.dtype),
				Snakes:        make(map[int32]SnakeRep, len(req.Snakes)),
				FoodLocations: make([]Coord, 0, len(req.FoodLocations)),
			}
			for k, v := range req.Snakes {
				step.Snakes[k] = SnakeRep{IsAlive: v.IsAlive, Length: v.Length}
			}
			for _, c := range req.FoodLocations {
				step.FoodLocations = append(step.FoodLocations, Coord{X: c.X, Y: c.Y})
			}
			dx, dy = s.snake.Update(step)
		}()
		fmt.Print("---STEP_END---\n")
		os.Stdout.Sync()
		if err := stream.Send(&pb.UpdateResponse{
			Direction: &pb.Coord{X: dx, Y: dy},
		}); err != nil {
			return err
		}
	}
}

func (s *snakeServer) Reset(_ context.Context, _ *pb.Empty) (*pb.Empty, error) {
	return &pb.Empty{}, nil
}
func (s *snakeServer) Kill(_ context.Context, _ *pb.Empty) (*pb.Empty, error) {
	return &pb.Empty{}, nil
}

func main() {
	lis, err := net.Listen("tcp", ":50051")
	if err != nil {
		fmt.Fprintf(os.Stderr, "listen: %v\n", err)
		os.Exit(1)
	}
	srv := grpc.NewServer()
	pb.RegisterRemoteSnakeServer(srv, newServer())
	if err := srv.Serve(lis); err != nil {
		fmt.Fprintf(os.Stderr, "serve: %v\n", err)
		os.Exit(1)
	}
}
