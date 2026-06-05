// setId(id)              — called once; your integer ID in this game
// setStartLength(n)      — called once; initial body length
// setStartPosition(pos)  — called once; initial head position Coord{ int x, int y }
// setInitData(data)      — called once; full environment metadata (see below)
//
// EnvInitData fields:
//   height, width              — int: grid dimensions
//   freeValue                  — int: cell value for empty space
//   blockedValue               — int: cell value for walls
//   foodValue                  — int: cell value for food
//   snakeTags                  — Map<Integer, String>: display name per snake id
//   snakeValues                — Map<Integer, SnakeValues>: { int headValue, int bodyValue } per snake id
//   startPositions             — Map<Integer, Coord>: { int x, int y } starting head position per snake id
//   baseMap                    — int[][]: static map (walls/free cells); row 0 is top
//
// update(data) — called every step; return int[] { dx, dy } to move:
//   { 1,  0} right   {-1,  0} left   { 0,  1} down   { 0, -1} up
//
// EnvStepData fields:
//   map                        — int[][]: current grid (walls + snakes + food); row 0 is top
//   snakes                     — Map<Integer, SnakeRep>: { boolean isAlive, int length } per snake id
//   foodLocations              — List<Coord>: food positions { int x, int y }

// Harness types (not editable):
//   Coord:       int x, y
//   SnakeValues: int headValue, bodyValue
//   SnakeRep:    boolean isAlive; int length
//   EnvInitData / EnvStepData fields documented above

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
