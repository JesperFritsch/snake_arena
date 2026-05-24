import type { Coord, EnvInitData, EnvStepData } from '../types';

export class Snake {
  setId(_id: number): void {}
  setStartLength(_length: number): void {}
  setStartPosition(_pos: Coord): void {}
  setInitData(_data: EnvInitData): void {}
  update(_data: EnvStepData): [number, number] { return [0, 0]; }
}
