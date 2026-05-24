// EnvInitData fields (set once before the first step):
//   height, width                    — grid dimensions
//   freeValue, blockedValue, foodValue — cell sentinel values
//   snakeTags.get(id)                — name string for each snake
//   snakeValues.get(id)              — SnakeValues(headValue, bodyValue)
//   startPositions.get(id)           — Coord(x, y) starting head position
//   baseMap[row][col]                — static map (walls/free); row 0 is top
//   baseMapDtype                     — numpy dtype string for raw bytes
//
// EnvStepData fields (every step):
//   map[row][col]    — full current grid (walls + snakes + food)
//   snakes.get(id)   — SnakeRep(isAlive, length) for each snake
//   foodLocations    — List<Coord> food positions
//
// update must return int[] { dx, dy }:
//   { 1,  0} = right   {-1,  0} = left
//   { 0,  1} = down    { 0, -1} = up

public class Snake implements SnakeInterface {
    private int id;
    private EnvInitData initData;

    public void setId(int id) { this.id = id; }
    public void setStartLength(int length) {}
    public void setStartPosition(Coord pos) {}
    public void setInitData(EnvInitData data) { this.initData = data; }

    public int[] update(EnvStepData data) {
        System.out.printf("step! grid=%dx%d food=%d%n",
            initData.height, initData.width, data.foodLocations.size());
        return new int[]{ 0, 1 }; // always move down — replace with your logic
    }
}
