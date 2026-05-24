public interface SnakeInterface {
    void setId(int id);
    void setStartLength(int n);
    void setStartPosition(Coord pos);
    void setInitData(EnvInitData data);
    int[] update(EnvStepData data);
}
