export interface Coord { x: number; y: number; }
export interface SnakeValues { head_value: number; body_value: number; }
export interface SnakeRep { is_alive: boolean; length: number; }

export interface EnvInitData {
  height: number;
  width: number;
  free_value: number;
  blocked_value: number;
  food_value: number;
  snake_tags: Record<number, string>;
  snake_values: Record<number, SnakeValues>;
  start_positions: Record<number, Coord>;
  base_map: number[][];
  base_map_dtype: string;
}

export interface EnvStepData {
  map: number[][];
  snakes: Record<number, SnakeRep>;
  food_locations: Coord[];
}

export interface SnakeInterface {
  setId(id: number): void;
  setStartLength(n: number): void;
  setStartPosition(pos: Coord): void;
  setInitData(data: EnvInitData): void;
  update(data: EnvStepData): [number, number];
}
