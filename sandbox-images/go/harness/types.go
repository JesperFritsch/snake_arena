package main

type SnakePlayer interface {
	SetId(id int32)
	SetStartLength(n int32)
	SetStartPosition(pos Coord)
	SetInitData(data EnvInitData)
	Update(data EnvStepData) (int32, int32)
}

type Coord struct{ X, Y int32 }
type SnakeValues struct{ HeadValue, BodyValue int32 }
type SnakeRep struct {
	IsAlive bool
	Length  int32
}

type EnvInitData struct {
	Height, Width                    int32
	FreeValue, BlockedValue, FoodValue int32
	SnakeTags      map[int32]string
	SnakeValues    map[int32]SnakeValues
	StartPositions map[int32]Coord
	BaseMap        [][]int32
	BaseMapDtype   string
}

type EnvStepData struct {
	Map           [][]int32
	Snakes        map[int32]SnakeRep
	FoodLocations []Coord
}
