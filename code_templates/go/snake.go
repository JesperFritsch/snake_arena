// SetId(id)              — called once; your integer ID in this game
// SetStartLength(n)      — called once; initial body length
// SetStartPosition(pos)  — called once; initial head position {X, Y}
// SetInitData(data)      — called once; full environment metadata (see below)
//
// EnvInitData fields:
//   Height, Width          — grid dimensions
//   FreeValue              — cell value for empty space
//   BlockedValue           — cell value for walls
//   FoodValue              — cell value for food
//   SnakeTags[id]          — display name for each snake
//   SnakeValues[id]        — { HeadValue, BodyValue }
//   StartPositions[id]     — { X, Y } starting head position
//   BaseMap[row][col]      — static map (walls/free cells); row 0 is top
//
// Update(data) — called every step; return (dx, dy) to move:
//   (1, 0) right   (-1, 0) left   (0, 1) down   (0, -1) up
//
// EnvStepData fields:
//   Map[row][col]          — current grid (walls + snakes + food)
//   Snakes[id]             — { IsAlive, Length }
//   FoodLocations          — []Coord food positions

package main

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
