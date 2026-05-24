package main

import "fmt"

// EnvInitData fields (set once before the first step):
//   Height, Width                    — grid dimensions
//   FreeValue, BlockedValue, FoodValue — cell sentinel values
//   SnakeTags[id]                    — name string for each snake
//   SnakeValues[id]                  — {HeadValue, BodyValue} for each snake
//   StartPositions[id]               — {X, Y} starting head position
//   BaseMap[row][col]                — static map (walls/free); row 0 is top
//   BaseMapDtype                     — numpy dtype string for raw bytes
//
// EnvStepData fields (every step):
//   Map[row][col]    — full current grid (walls + snakes + food)
//   Snakes[id]       — {IsAlive, Length} for each snake
//   FoodLocations    — []Coord food positions
//
// Update must return (dx, dy):
//   ( 1,  0) = right   (-1,  0) = left
//   ( 0,  1) = down    ( 0, -1) = up

type Snake struct {
	id       int32
	initData EnvInitData
}

func NewSnake() *Snake { return &Snake{} }

func (s *Snake) SetId(id int32)                { s.id = id }
func (s *Snake) SetStartLength(_ int32)        {}
func (s *Snake) SetStartPosition(_ Coord)      {}
func (s *Snake) SetInitData(data EnvInitData)  { s.initData = data }

func (s *Snake) Update(data EnvStepData) (int32, int32) {
	fmt.Printf("step! grid=%dx%d food=%d\n",
		s.initData.Height, s.initData.Width, len(data.FoodLocations))
	return 0, 1 // always move down — replace with your logic
}
