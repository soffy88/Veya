/** Iframe DOM capture script for Design Mode (same-origin only).

Injected into the iframe when Design Mode is active. Communicates with parent
via postMessage. Provides element highlight overlay and click capture.
*/

export interface DesignCapture {
  outerHTML: string;
  selectorPath: string;
  computedStyles: ComputedStyles;
  boundingBox: BoundingBox;
  elementScreenshotBase64: string;
  pageUrl: string;
  pageTitle: string;
  sourceLocation: SourceLocation;
}

export interface ComputedStyles {
  boxModel: Record<string, string>;
  display: string;
  flex: Record<string, string> | null;
  grid: Record<string, string> | null;
  font: Record<string, string>;
  color: Record<string, string>;
  position: Record<string, string>;
}

export interface BoundingBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface SourceLocation {
  type: "veya" | "react" | "generic" | "unknown";
  file: string;
  line: number;
  column?: number;
  rawAttribute?: string;
}

interface CaptureMessage {
  type: "capture";
  selector: string;
  requestId: string;
}

interface HighlightMessage {
  type: "highlight";
  selector: string | null;
}

interface CaptureResponseMessage {
  type: "captureResponse";
  requestId: string;
  capture: DesignCapture | null;
  error?: string;
}

interface ReadyMessage {
  type: "ready";
}

const MAX_OUTER_HTML = 8 * 1024;

class DesignModeCapture {
  private highlightEl: HTMLElement | null = null;
  private pendingRequests = new Map<string, (capture: DesignCapture | null, error?: string) => void>();
  private messageId = 0;
  private isActive = false;
  private currentHighlightSelector: string | null = null;

  constructor() {
    if (typeof window !== "undefined") {
      window.addEventListener("message", this.handleMessage.bind(this));
      // Notify parent that capture script is ready
      this.postToParent({ type: "ready" } as ReadyMessage);
    }
  }

  private handleMessage(event: MessageEvent): void {
    // Only accept messages from parent
    if (event.source !== window.parent) return;

    const data = event.data;
    if (!data || typeof data.type !== "string") return;

    switch (data.type) {
      case "activate":
        this.activate();
        break;
      case "deactivate":
        this.deactivate();
        break;
      case "capture":
        this.captureElement(data as CaptureMessage);
        break;
      case "highlight":
        this.highlightElement((data as HighlightMessage).selector);
        break;
    }
  }

  private postToParent(message: object): void {
    window.parent.postMessage(message, "*");
  }

  activate(): void {
    this.isActive = true;
    // Add hover listeners for live highlighting
    document.addEventListener("mouseover", this.handleMouseOver.bind(this), true);
    document.addEventListener("mouseout", this.handleMouseOut.bind(this), true);
    document.addEventListener("click", this.handleClick.bind(this), true);
  }

  deactivate(): void {
    this.isActive = false;
    document.removeEventListener("mouseover", this.handleMouseOver.bind(this), true);
    document.removeEventListener("mouseout", this.handleMouseOut.bind(this), true);
    document.removeEventListener("click", this.handleClick.bind(this), true);
    this.clearHighlight();
  }

  private handleMouseOver(e: MouseEvent): void {
    if (!this.isActive) return;
    const target = e.target as HTMLElement;
    if (target === this.highlightEl) return;
    const selector = this.getSelectorPath(target);
    this.highlightElement(selector);
  }

  private handleMouseOut(e: MouseEvent): void {
    if (!this.isActive) return;
    // Only clear if mouse actually left the highlighted element
    const target = e.target as HTMLElement;
    if (target === this.highlightEl) return;
    // Check if related target is inside the highlighted element
    const related = e.relatedTarget as HTMLElement | null;
    if (related && this.highlightEl && this.highlightEl.contains(related)) return;
    this.clearHighlight();
  }

  private handleClick(e: MouseEvent): void {
    if (!this.isActive) return;
    e.preventDefault();
    e.stopPropagation();
    const target = e.target as HTMLElement;
    const selector = this.getSelectorPath(target);
    this.captureAndSend(selector);
  }

  private captureAndSend(selector: string): void {
    const requestId = `capture_${++this.messageId}`;
    this.captureElement({ type: "capture", selector, requestId } as CaptureMessage);
  }

  private captureElement(msg: CaptureMessage): void {
    const { selector, requestId } = msg;
    try {
      const element = document.querySelector(selector);
      if (!element) {
        this.sendResponse(requestId, null, `Element not found: ${selector}`);
        return;
      }

      const capture = this.buildCapture(element, selector);
      this.sendResponse(requestId, capture);
    } catch (err) {
      this.sendResponse(requestId, null, err instanceof Error ? err.message : String(err));
    }
  }

  private buildCapture(element: Element, selector: string): DesignCapture {
    const outerHTML = element.outerHTML;
    const trimmedHTML = outerHTML.length > MAX_OUTER_HTML ? outerHTML.slice(0, MAX_OUTER_HTML) + "\n…[trimmed]" : outerHTML;

    const computedStyles = this.getComputedStyles(element);
    const boundingBox = this.getBoundingBox(element);
    const screenshotBase64 = this.captureElementScreenshot(element);
    const sourceLocation = this.getSourceLocation(element);

    return {
      outerHTML: trimmedHTML,
      selectorPath: selector,
      computedStyles,
      boundingBox,
      elementScreenshotBase64: screenshotBase64,
      pageUrl: window.location.href,
      pageTitle: document.title,
      sourceLocation,
    };
  }

  private getComputedStyles(element: Element): ComputedStyles {
    const cs = window.getComputedStyle(element);
    return {
      boxModel: {
        margin: `${cs.marginTop} ${cs.marginRight} ${cs.marginBottom} ${cs.marginLeft}`,
        border: `${cs.borderTopWidth} ${cs.borderRightWidth} ${cs.borderBottomWidth} ${cs.borderLeftWidth}`,
        padding: `${cs.paddingTop} ${cs.paddingRight} ${cs.paddingBottom} ${cs.paddingLeft}`,
        width: cs.width,
        height: cs.height,
      },
      display: cs.display,
      flex: cs.display === "flex" || cs.display === "inline-flex" ? {
        direction: cs.flexDirection,
        justify: cs.justifyContent,
        align: cs.alignItems,
        gap: cs.gap,
      } : null,
      grid: cs.display === "grid" || cs.display === "inline-grid" ? {
        templateColumns: cs.gridTemplateColumns,
        templateRows: cs.gridTemplateRows,
        gap: cs.gap,
      } : null,
      font: {
        family: cs.fontFamily,
        size: cs.fontSize,
        weight: cs.fontWeight,
        lineHeight: cs.lineHeight,
      },
      color: {
        color: cs.color,
        backgroundColor: cs.backgroundColor,
      },
      position: {
        position: cs.position,
        top: cs.top,
        right: cs.right,
        bottom: cs.bottom,
        left: cs.left,
        zIndex: cs.zIndex,
      },
    };
  }

  private getBoundingBox(element: Element): BoundingBox {
    const rect = element.getBoundingClientRect();
    return {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
    };
  }

  private captureElementScreenshot(element: Element): string {
    // Create a canvas and draw the element to it
    const rect = element.getBoundingClientRect();
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) return "";

    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(rect.width * scale));
    canvas.height = Math.max(1, Math.round(rect.height * scale));
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    ctx.scale(scale, scale);

    // Use html2canvas-like approach: draw element via SVG foreignObject
    const svg = new XMLSerializer().serializeToString(this.elementToSVG(element));
    const img = new Image();
    const svgBlob = new Blob([svg], { type: "image/svg+xml" });
    const url = URL.createObjectURL(svgBlob);

    // Since we can't easily wait for async here, return a placeholder
    // The actual screenshot will be captured via the server-side path
    // For iframe path, we return empty and rely on server-side screenshot
    URL.revokeObjectURL(url);
    return ""; // Placeholder - server-side will provide screenshot
  }

  private elementToSVG(element: Element): SVGElement {
    const rect = element.getBoundingClientRect();
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("width", rect.width.toString());
    svg.setAttribute("height", rect.height.toString());
    svg.setAttribute("viewBox", `0 0 ${rect.width} ${rect.height}`);

    const foreignObject = document.createElementNS(svgNS, "foreignObject");
    foreignObject.setAttribute("width", "100%");
    foreignObject.setAttribute("height", "100%");
    foreignObject.setAttribute("x", "0");
    foreignObject.setAttribute("y", "0");

    const clone = element.cloneNode(true) as HTMLElement;
    // Make sure clone is positioned at origin
    clone.style.position = "absolute";
    clone.style.top = "0";
    clone.style.left = "0";
    clone.style.margin = "0";
    clone.style.transform = "none";

    foreignObject.appendChild(clone);
    svg.appendChild(foreignObject);
    return svg;
  }

  private getSourceLocation(element: Element): SourceLocation {
    // Priority 1: data-veya-src
    if (element.hasAttribute("data-veya-src")) {
      const val = element.getAttribute("data-veya-src") || "";
      const [file, line] = val.split(":");
      return { type: "veya", file: file || "", line: parseInt(line) || 0, rawAttribute: val };
    }
    // Priority 2: data-source
    if (element.hasAttribute("data-source")) {
      const val = element.getAttribute("data-source") || "";
      const [file, line] = val.split(":");
      return { type: "generic", file: file || "", line: parseInt(line) || 0, rawAttribute: val };
    }
    // Priority 3: React __source
    const reactSource = (element as any).__source;
    if (reactSource && reactSource.fileName) {
      return { type: "react", file: reactSource.fileName, line: reactSource.lineNumber || 0 };
    }
    return { type: "unknown", file: "", line: 0 };
  }

  private getSelectorPath(element: Element): string {
    const path: string[] = [];
    let current: Element | null = element;
    while (current && current !== document.body) {
      let selector = current.tagName.toLowerCase();
      if (current.id) {
        selector += "#" + current.id;
        path.unshift(selector);
        break;
      } else {
        const parent = current.parentElement;
        if (parent) {
          const tagName = current.tagName;
          const siblings = Array.from(parent.children).filter(
            (c): c is Element => c.tagName === tagName
          );
          if (siblings.length > 1) {
            const index = siblings.indexOf(current) + 1;
            selector += ":nth-of-type(" + index + ")";
          }
        }
      }
      path.unshift(selector);
      current = current.parentElement;
    }
    return path.join(" > ");
  }

  private highlightElement(selector: string | null): void {
    this.clearHighlight();
    if (!selector) return;

    try {
      const element = document.querySelector(selector);
      if (!element) return;

      const rect = element.getBoundingClientRect();
      this.highlightEl = document.createElement("div");
      this.highlightEl.style.cssText = `
        position: fixed;
        left: ${rect.left}px;
        top: ${rect.top}px;
        width: ${rect.width}px;
        height: ${rect.height}px;
        border: 2px solid #06b6d4;
        background: rgba(6, 182, 212, 0.15);
        pointer-events: none;
        z-index: 2147483647;
        box-sizing: border-box;
        transition: all 50ms ease-out;
      `;
      document.body.appendChild(this.highlightEl);
      this.currentHighlightSelector = selector;
    } catch {
      // Ignore highlight errors
    }
  }

  private clearHighlight(): void {
    if (this.highlightEl) {
      this.highlightEl.remove();
      this.highlightEl = null;
      this.currentHighlightSelector = null;
    }
  }

  private sendResponse(requestId: string, capture: DesignCapture | null, error?: string): void {
    this.postToParent({
      type: "captureResponse",
      requestId,
      capture,
      error,
    } as CaptureResponseMessage);
  }
}

// Auto-initialize when loaded in iframe
if (typeof window !== "undefined" && window !== window.parent) {
  new DesignModeCapture();
}

export { DesignModeCapture };