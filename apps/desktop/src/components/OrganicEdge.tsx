/**
 * OrganicEdge — an S-curve edge component.
 *
 * The third layout mode ("Organic") keeps Compact's node placement but
 * swaps React Flow's default straight-ish routing for a cubic-bezier
 * S-curve. The two control points sit on opposite sides of the straight
 * line between source and target, which:
 *
 *   1. Spreads visually-parallel edges apart (a fan-out from one agent
 *      to three tools no longer stacks into a single thick line).
 *   2. Alternates bend direction per edge via a stable id-derived seed,
 *      so adjacent edges curve opposite ways instead of piling on the
 *      same side.
 *
 * The seam this fits into: everything about *positions* stays in
 * `lib/layout.ts`; everything about *edge appearance* stays here. If we
 * ever add a fourth mode (radial, force-directed, whatever), the split
 * is unchanged — new layout goes in layout.ts, new routing goes as
 * another edge component.
 */

import { BaseEdge, EdgeLabelRenderer, type EdgeProps } from "@xyflow/react";

/** Deterministic 1-bit hash from an edge id. Same id → same bend
 *  direction across re-renders, but adjacent parallel edges pick
 *  opposite sides because their ids differ by the incrementing suffix. */
function seedFromId(id: string): number {
  let h = 0;
  for (let i = 0; i < id.length; i++) {
    // xor with char code, mix — cheap, stable, no crypto needed.
    h = (h * 31 + id.charCodeAt(i)) | 0;
  }
  return h;
}

/** Build the S-curve path and the label anchor (t=0.5 on the cubic). */
function organicPath(
  sx: number,
  sy: number,
  tx: number,
  ty: number,
  seed: number,
): { d: string; midX: number; midY: number } {
  const dx = tx - sx;
  const dy = ty - sy;
  const len = Math.hypot(dx, dy);
  // Degenerate case: co-located handles. Draw a straight line to avoid NaN.
  if (len < 1) {
    return { d: `M ${sx} ${sy} L ${tx} ${ty}`, midX: (sx + tx) / 2, midY: (sy + ty) / 2 };
  }
  // Perpendicular unit vector to (dx, dy).
  const px = -dy / len;
  const py = dx / len;
  // Amplitude: proportional to distance, capped so long swoops don't
  // arc across the whole canvas. 25% of the run is enough to read as
  // curvy without breaking spatial intuition of where the edge ends up.
  const amp = Math.min(50, len * 0.25);
  // Alternate side per edge so parallel siblings don't all bend the
  // same way (which would just re-stack them on the curved side).
  const sign = seed & 1 ? 1 : -1;
  // Control points sit on OPPOSITE sides of the line — this is what
  // makes it an S instead of a single-arch parabola.
  const c1x = sx + dx * 0.35 + px * amp * sign;
  const c1y = sy + dy * 0.35 + py * amp * sign;
  const c2x = sx + dx * 0.65 - px * amp * sign;
  const c2y = sy + dy * 0.65 - py * amp * sign;
  // Bezier midpoint at t=0.5: (P0 + 3*P1 + 3*P2 + P3) / 8. Used to
  // anchor the "delegates" label at a stable spot on the curve.
  const midX = (sx + 3 * c1x + 3 * c2x + tx) / 8;
  const midY = (sy + 3 * c1y + 3 * c2y + ty) / 8;
  return { d: `M ${sx} ${sy} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${tx} ${ty}`, midX, midY };
}

/**
 * The component itself. Mirrors the behaviors the default `smoothstep`
 * edge gives us today: honors `style` (stroke color/width/dasharray),
 * honors `markerEnd`, and renders an optional label with the same
 * background/padding/radius props the current edge factory passes.
 *
 * `animated` (used on delegates_to) rides for free: React Flow adds an
 * `.animated` class to the containing `<g>` and its CSS keyframe
 * animates `stroke-dashoffset` on any `.react-flow__edge-path` inside —
 * which is exactly the class `<BaseEdge>` renders.
 */
export function OrganicEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  style,
  markerEnd,
  label,
  labelStyle,
  labelBgStyle,
  labelBgPadding,
  labelBgBorderRadius,
}: EdgeProps) {
  const { d, midX, midY } = organicPath(sourceX, sourceY, targetX, targetY, seedFromId(id));

  return (
    <>
      <BaseEdge id={id} path={d} markerEnd={markerEnd} style={style} />
      {label != null && (
        <EdgeLabelRenderer>
          <div
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${midX}px, ${midY}px)`,
              // Bg + padding + radius come from the caller's labelBg* props;
              // we spread them so the "delegates" pill keeps its current look.
              ...(labelBgStyle ?? {}),
              padding: labelBgPadding
                ? `${labelBgPadding[1]}px ${labelBgPadding[0]}px`
                : undefined,
              borderRadius: labelBgBorderRadius,
              // Label typography comes from labelStyle (color/size/weight).
              ...(labelStyle ?? {}),
              pointerEvents: "all",
              // React Flow's default label font-size if the caller didn't set one.
              fontSize: labelStyle?.fontSize ?? 12,
              // Prevent the pill from breaking the S-curve at label position.
              whiteSpace: "nowrap",
            }}
            className="nodrag nopan"
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
