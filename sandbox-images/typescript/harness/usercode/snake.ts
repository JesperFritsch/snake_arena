import type { Coord, EnvInitData, EnvStepData, SnakeInterface } from '../types';

class Snake implements SnakeInterface {
  setId(_id: number): void {}
  setStartLength(_n: number): void {}
  setStartPosition(_pos: Coord): void {}
  setInitData(_data: EnvInitData): void {}
  update(_data: EnvStepData): [number, number] { return [0, 0]; }
}

export function createSnake(): SnakeInterface { return new Snake(); }
