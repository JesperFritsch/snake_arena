package main

type Snake struct{}

func NewSnake() SnakePlayer { return &Snake{} }

func (s *Snake) SetId(_ int32)            {}
func (s *Snake) SetStartLength(_ int32)   {}
func (s *Snake) SetStartPosition(_ Coord) {}
func (s *Snake) SetInitData(_ EnvInitData) {}
func (s *Snake) Update(_ EnvStepData) (int32, int32) { return 0, 0 }
