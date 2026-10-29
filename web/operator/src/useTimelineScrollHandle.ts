import { type RefObject, useEffect } from "react";

/** Trackless draggable handle shared by run and deployment timelines. */
export function useTimelineScrollHandle(
  scrollElement: RefObject<HTMLDivElement | null>,
  scrollHandle: RefObject<HTMLDivElement | null>,
  expanded: boolean,
  active = true,
) {
  useEffect(() => {
    const viewport = scrollElement.current;
    const handle = scrollHandle.current;
    if (!viewport || !handle) return;
    let drag: { pointerId: number; y: number; scrollTop: number } | undefined;
    const update = () => {
      const { clientHeight, scrollHeight, scrollTop } = viewport;
      const trackHeight = Math.max(0, clientHeight - 8);
      const height = Math.min(
        trackHeight,
        Math.max(24, (clientHeight / scrollHeight) * trackHeight),
      );
      const overflow = scrollHeight - clientHeight;
      handle.hidden = overflow <= 0;
      handle.style.height = `${height}px`;
      handle.style.transform = `translateY(${overflow > 0 ? (scrollTop / overflow) * (trackHeight - height) : 0}px)`;
    };
    const startDrag = (event: PointerEvent) => {
      if (event.button !== 0) return;
      event.preventDefault();
      drag = { pointerId: event.pointerId, y: event.clientY, scrollTop: viewport.scrollTop };
      handle.setPointerCapture(event.pointerId);
      handle.dataset.dragging = "true";
    };
    const moveDrag = (event: PointerEvent) => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      const travel = viewport.clientHeight - 8 - handle.clientHeight;
      if (travel <= 0) return;
      viewport.scrollTop =
        drag.scrollTop +
        ((event.clientY - drag.y) / travel) * (viewport.scrollHeight - viewport.clientHeight);
    };
    const endDrag = () => {
      drag = undefined;
      delete handle.dataset.dragging;
    };
    const observer = new ResizeObserver(update);
    observer.observe(viewport);
    observer.observe(viewport.firstElementChild!);
    viewport.addEventListener("scroll", update, { passive: true });
    handle.addEventListener("pointerdown", startDrag);
    handle.addEventListener("pointermove", moveDrag);
    handle.addEventListener("lostpointercapture", endDrag);
    update();
    return () => {
      observer.disconnect();
      viewport.removeEventListener("scroll", update);
      handle.removeEventListener("pointerdown", startDrag);
      handle.removeEventListener("pointermove", moveDrag);
      handle.removeEventListener("lostpointercapture", endDrag);
    };
  }, [expanded, active, scrollElement, scrollHandle]);
}
