import java.util.List;
import java.util.Map;

public class EnvStepData {
    public final int[][] map;
    public final Map<Integer, SnakeRep> snakes;
    public final List<Coord> foodLocations;

    public EnvStepData(int[][] map, Map<Integer, SnakeRep> snakes, List<Coord> foodLocations) {
        this.map = map;
        this.snakes = snakes;
        this.foodLocations = foodLocations;
    }
}
