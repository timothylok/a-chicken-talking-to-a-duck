import type { TrafficLight as TrafficLightValue } from "../lib/types";

export default function TrafficLight({ light }: { light: TrafficLightValue | null }) {
  return <span className={`dot ${light ?? "na"}`} title={light ?? "N/A"} />;
}
