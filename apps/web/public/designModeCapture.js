/** Iframe DOM capture script for Design Mode (same-origin only).

Injected into the iframe when Design Mode is active. Communicates with parent
via postMessage. Provides element highlight overlay and click capture.
*/

(function() {
  'use strict';

  const MAX_OUTER_HTML = 8 * 1024;
  let highlightEl = null;
  let isActive = false;
  let messageId = 0;
  const pendingRequests = new Map();

  function postToParent(message) {
    window.parent.postMessage(message, '*');
  }

  function getSelectorPath(element) {
    const path = [];
    let current = element;
    while (current && current !== document.body) {
      let selector = current.tagName.toLowerCase();
      if (current.id) {
        selector += '#' + current.id;
        path.unshift(selector);
        break;
      } else {
        const parent = current.parentElement;
        if (parent) {
          const siblings = Array.from(parent.children).filter(
            c => c.tagName === current.tagName
          );
          if (siblings.length > 1) {
            const index = siblings.indexOf(current) + 1;
            selector += ':nth-of-type(' + index + ')';
          }
        }
      }
      path.unshift(selector);
      current = current.parentElement;
    }
    return path.join(' > ');
  }

  function getComputedStyles(element) {
    const cs = window.getComputedStyle(element);
    return {
      boxModel: {
        margin: cs.marginTop + ' ' + cs.marginRight + ' ' + cs.marginBottom + ' ' + cs.marginLeft,
        border: cs.borderTopWidth + ' ' + cs.borderRightWidth + ' ' + cs.borderBottomWidth + ' ' + cs.borderLeftWidth,
        padding: cs.paddingTop + ' ' + cs.paddingRight + ' ' + cs.paddingBottom + ' ' + cs.paddingLeft,
        width: cs.width,
        height: cs.height,
      },
      display: cs.display,
      flex: (cs.display === 'flex' || cs.display === 'inline-flex') ? {
        direction: cs.flexDirection,
        justify: cs.justifyContent,
        align: cs.alignItems,
        gap: cs.gap,
      } : null,
      grid: (cs.display === 'grid' || cs.display === 'inline-grid') ? {
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

  function getBoundingBox(element) {
    const rect = element.getBoundingClientRect();
    return {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
    };
  }

  function getSourceLocation(element) {
    // Priority 1: data-veya-src
    if (element.hasAttribute('data-veya-src')) {
      const val = element.getAttribute('data-veya-src') || '';
      const [file, line] = val.split(':');
      return { type: 'veya', file: file || '', line: parseInt(line) || 0, rawAttribute: val };
    }
    // Priority 2: data-source
    if (element.hasAttribute('data-source')) {
      const val = element.getAttribute('data-source') || '';
      const [file, line] = val.split(':');
      return { type: 'generic', file: file || '', line: parseInt(line) || 0, rawAttribute: val };
    }
    // Priority 3: React __source
    const reactSource = element.__source;
    if (reactSource && reactSource.fileName) {
      return { type: 'react', file: reactSource.fileName, line: reactSource.lineNumber || 0 };
    }
    return { type: 'unknown', file: '', line: 0 };
  }

  function buildCapture(element, selector) {
    const outerHTML = element.outerHTML;
    const trimmedHTML = outerHTML.length > MAX_OUTER_HTML ? outerHTML.slice(0, MAX_OUTER_HTML) + '\n...[trimmed]' : outerHTML;

    // For iframe capture, we can't easily get a screenshot without html2canvas
    // Return empty - server-side will provide screenshot
    return {
      outerHTML: trimmedHTML,
      selectorPath: selector,
      computedStyles: getComputedStyles(element),
      boundingBox: getBoundingBox(element),
      elementScreenshotBase64: '',
      pageUrl: window.location.href,
      pageTitle: document.title,
      sourceLocation: getSourceLocation(element),
    };
  }

  function highlightElement(selector) {
    clearHighlight();
    if (!selector) return;

    try {
      const element = document.querySelector(selector);
      if (!element) return;

      const rect = element.getBoundingClientRect();
      highlightEl = document.createElement('div');
      highlightEl.style.cssText = `
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
      document.body.appendChild(highlightEl);
    } catch (e) {
      // Ignore highlight errors
    }
  }

  function clearHighlight() {
    if (highlightEl) {
      highlightEl.remove();
      highlightEl = null;
    }
  }

  function handleMouseOver(e) {
    if (!isActive) return;
    const target = e.target;
    if (target === highlightEl) return;
    const selector = getSelectorPath(target);
    highlightElement(selector);
  }

  function handleMouseOut(e) {
    if (!isActive) return;
    const target = e.target;
    if (target === highlightEl) return;
    const related = e.relatedTarget;
    if (related && highlightEl && highlightEl.contains(related)) return;
    clearHighlight();
  }

  function handleClick(e) {
    if (!isActive) return;
    e.preventDefault();
    e.stopPropagation();
    const target = e.target;
    const selector = getSelectorPath(target);
    captureAndSend(selector);
  }

  function captureAndSend(selector) {
    const requestId = 'capture_' + (++messageId);
    const element = document.querySelector(selector);
    if (!element) {
      sendResponse(requestId, null, 'Element not found: ' + selector);
      return;
    }

    try {
      const capture = buildCapture(element, selector);
      sendResponse(requestId, capture);
    } catch (err) {
      sendResponse(requestId, null, err instanceof Error ? err.message : String(err));
    }
  }

  function sendResponse(requestId, capture, error) {
    postToParent({
      type: 'captureResponse',
      requestId: requestId,
      capture: capture,
      error: error,
    });
  }

  function activate() {
    isActive = true;
    document.addEventListener('mouseover', handleMouseOver, true);
    document.addEventListener('mouseout', handleMouseOut, true);
    document.addEventListener('click', handleClick, true);
  }

  function deactivate() {
    isActive = false;
    document.removeEventListener('mouseover', handleMouseOver, true);
    document.removeEventListener('mouseout', handleMouseOut, true);
    document.removeEventListener('click', handleClick, true);
    clearHighlight();
  }

  function handleMessage(event) {
    if (event.source !== window.parent) return;
    const data = event.data;
    if (!data || typeof data.type !== 'string') return;

    switch (data.type) {
      case 'activate':
        activate();
        break;
      case 'deactivate':
        deactivate();
        break;
      case 'capture':
        captureAndSend(data.selector);
        break;
    }
  }

  window.addEventListener('message', handleMessage);

  // Notify parent that capture script is ready
  postToParent({ type: 'ready' });

  // Expose for debugging
  window.__VEYA_DESIGN_CAPTURE__ = { activate, deactivate, captureAndSend };
})();