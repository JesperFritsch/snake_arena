// setId(id)              — called once; your integer ID in this game
// setStartLength(n)      — called once; initial body length
// setStartPosition(pos)  — called once; initial head position {x, y}
// setInitData(data)      — called once; full environment metadata (see below)
//
// EnvInitData fields:
//   height, width              — grid dimensions
//   freeValue                  — cell value for empty space
//   blockedValue               — cell value for walls
//   foodValue                  — cell value for food
//   snakeTags.get(id)          — display name for each snake
//   snakeValues.get(id)        — SnakeValues(headValue, bodyValue)
//   startPositions.get(id)     — Coord(x, y) starting head position
//   baseMap[row][col]          — static map (walls/free cells); row 0 is top
//
// update(data) — called every step; return int[] { dx, dy } to move:
//   { 1,  0} right   {-1,  0} left   { 0,  1} down   { 0, -1} up
//
// EnvStepData fields:
//   map[row][col]              — current grid (walls + snakes + food)
//   snakes.get(id)             — SnakeRep(isAlive, length)
//   foodLocations              — List<Coord> food positions

public class Snake implements SnakeInterface {
    private int id;
    private EnvInitData initData;

    @Override public void setId(int id)               { this.id = id; }
    @Override public void setStartLength(int n)       {}
    @Override public void setStartPosition(Coord pos) {}
    @Override public void setInitData(EnvInitData data) { this.initData = data; }

    @Override public int[] update(EnvStepData data) {
        System.out.printf("step! grid=%dx%d food=%d%n",
            initData.height, initData.width, data.foodLocations.size());
        return new int[]{ 0, 1 }; // always move down — replace with your logic
    }
}
