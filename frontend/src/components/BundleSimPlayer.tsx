import { useBundleSource } from "../sim/useBundleSource";
import { SimPlayer } from "./SimPlayer";

interface Props {
  /** Stable key so the source resets when the parent picks a different replay. */
  bundleKey: string | number;
  getBundleUrl: () => Promise<{ url: string }>;
  participantNames: string[];
}

/** Plays a finished sim run from its bundle zip — no live connection. */
export function BundleSimPlayer({ bundleKey, getBundleUrl, participantNames }: Props) {
  const source = useBundleSource({ getBundleUrl, resetKey: bundleKey });
  return (
    <SimPlayer
      source={source}
      participantNames={participantNames}
      showExecTimesBar
    />
  );
}
