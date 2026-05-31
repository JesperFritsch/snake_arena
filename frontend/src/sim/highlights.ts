// Timeline markers parsed from analysis.json (deaths + trap events).
// `snakeIdx` is the sim snake_id — translate to seat via SimStore.seatBySnakeId
// before joining against participant names / colors.

export interface Highlight {
  step: number;
  kind: "death" | "trap";
  snakeIdx: number;
  trappingIdx?: number;
}

export function parseHighlights(data: Uint8Array): Highlight[] {
  const analysis = JSON.parse(new TextDecoder().decode(data)) as {
    fatal_steps?: Record<string, number>;
    traps_mapping?: Record<string, Array<{ trapped_ids: number[]; trapping_ids: number[] }>>;
  };
  const out: Highlight[] = [];
  for (const [id, step] of Object.entries(analysis.fatal_steps ?? {})) {
    out.push({ step, kind: "death", snakeIdx: Number(id) });
  }
  for (const [stepStr, infos] of Object.entries(analysis.traps_mapping ?? {})) {
    const step = Number(stepStr);
    for (const t of infos) {
      out.push({ step, kind: "trap", snakeIdx: t.trapped_ids[0] ?? 0, trappingIdx: t.trapping_ids[0] });
    }
  }
  return out;
}
