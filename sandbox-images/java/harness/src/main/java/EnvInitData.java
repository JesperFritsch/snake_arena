import java.util.Map;

public class EnvInitData {
    public final int height, width;
    public final int freeValue, blockedValue, foodValue;
    public final Map<Integer, String> snakeTags;
    public final Map<Integer, SnakeValues> snakeValues;
    public final Map<Integer, Coord> startPositions;
    public final int[][] baseMap;
    public final String baseMapDtype;

    public EnvInitData(
        int height, int width,
        int freeValue, int blockedValue, int foodValue,
        Map<Integer, String> snakeTags,
        Map<Integer, SnakeValues> snakeValues,
        Map<Integer, Coord> startPositions,
        int[][] baseMap, String baseMapDtype
    ) {
        this.height = height;
        this.width = width;
        this.freeValue = freeValue;
        this.blockedValue = blockedValue;
        this.foodValue = foodValue;
        this.snakeTags = snakeTags;
        this.snakeValues = snakeValues;
        this.startPositions = startPositions;
        this.baseMap = baseMap;
        this.baseMapDtype = baseMapDtype;
    }
}
