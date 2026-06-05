// SetId(id)              — called once; your integer ID in this game
// SetStartLength(n)      — called once; initial body length
// SetStartPosition(pos)  — called once; initial head position Coord{X, Y int32}
// SetInitData(data)      — called once; full environment metadata (see below)
//
// EnvInitData fields:
//   Height, Width          — int32: grid dimensions
//   FreeValue              — int32: cell value for empty space
//   BlockedValue           — int32: cell value for walls
//   FoodValue              — int32: cell value for food
//   SnakeTags              — map[int32]string: display name per snake id
//   SnakeValues            — map[int32]SnakeValues: {HeadValue, BodyValue int32} per snake id
//   StartPositions         — map[int32]Coord: {X, Y int32} starting head position per snake id
//   BaseMap                — [][]int32: static map (walls/free cells); row 0 is top
//
// Update(data) — called every step; return (dx, dy) to move:
//   (1, 0) right   (-1, 0) left   (0, 1) down   (0, -1) up
//
// EnvStepData fields:
//   Map                    — [][]int32: current grid (walls + snakes + food); row 0 is top
//   Snakes                 — map[int32]SnakeRep: {IsAlive bool, Length int32} per snake id
//   FoodLocations          — []Coord: food positions {X, Y int32}

package main

// Harness types (from types.go, not editable):
//   type Coord       struct{ X, Y int32 }
//   type SnakeValues struct{ HeadValue, BodyValue int32 }
//   type SnakeRep    struct{ IsAlive bool; Length int32 }
//   EnvInitData / EnvStepData fields documented above

import "fmt"

type Snake struct {
	id       int32
	initData EnvInitData
}

func NewSnake() SnakePlayer { return &Snake{} }

func (s *Snake) SetId(id int32)               { s.id = id }
func (s *Snake) SetStartLength(_ int32)       {}
func (s *Snake) SetStartPosition(_ Coord)     {}
func (s *Snake) SetInitData(data EnvInitData) { s.initData = data }

func (s *Snake) Update(data EnvStepData) (int32, int32) {
	fmt.Printf("step! grid=%dx%d food=%d\n",
		s.initData.Height, s.initData.Width, len(data.FoodLocations))
	return 0, 1 // always move down — replace with your logic
}
