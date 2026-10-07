/**
 * Persistent tdc.js execution worker.
 *
 * stdin/stdout use one JSON object per line. New callers should send an
 * `events` array; legacy `trajectory` and `clicks` inputs remain supported.
 */
'use strict';

const NATIVE_TO_STRING_MAP = new WeakMap();
const ORIGINAL_FUNCTION_TO_STRING = Function.prototype.toString;
let NATIVE_TO_STRING_INSTALLED = false;

function ensureNativeToStringInstalled() {
    if (NATIVE_TO_STRING_INSTALLED) return;
    Function.prototype.toString = function toString() {
        if (NATIVE_TO_STRING_MAP.has(this)) return NATIVE_TO_STRING_MAP.get(this);
        if (this === Function.prototype.toString) return 'function toString() { [native code] }';
        return ORIGINAL_FUNCTION_TO_STRING.call(this);
    };
    NATIVE_TO_STRING_MAP.set(Function.prototype.toString, 'function toString() { [native code] }');
    NATIVE_TO_STRING_INSTALLED = true;
}

function installNativeToString(target, names) {
    ensureNativeToStringInstalled();
    for (const name of names) {
        try {
            const value = target[name];
            if (typeof value === 'function') {
                NATIVE_TO_STRING_MAP.set(value, 'function ' + name + '() { [native code] }');
            }
        } catch (_) {}
    }
}

function sanitizeStack(value) {
    return String(value || '')
        .replace(/\((?:[A-Za-z]:)?[^)\n]*node[^)\n]*\)/gi, '(native)')
        .replace(/at\s+node:.+/gi, 'at native')
        .replace(/at\s+internal\/.+/gi, 'at native')
        .replace(/at\s+.*node_modules.+/gi, 'at native')
        .replace(/\(node:[^)]+\)/gi, '(native)');
}

let STACK_SANITIZE_INSTALLED = false;
function ensureStackSanitizeInstalled() {
    if (STACK_SANITIZE_INSTALLED) return;
    const originalPrepareStackTrace = Error.prepareStackTrace;
    Error.prepareStackTrace = function prepareStackTrace(err, stack) {
        const raw = originalPrepareStackTrace
            ? originalPrepareStackTrace(err, stack)
            : String(err) + '\n' + stack.map((frame) => '    at ' + frame).join('\n');
        return sanitizeStack(raw);
    };
    const stackDesc = Object.getOwnPropertyDescriptor(Error.prototype, 'stack');
    if (stackDesc && stackDesc.get) {
        Object.defineProperty(Error.prototype, 'stack', {
            configurable: true,
            enumerable: false,
            get() { return sanitizeStack(stackDesc.get.call(this)); },
            set(value) { if (stackDesc.set) stackDesc.set.call(this, value); },
        });
    }
    STACK_SANITIZE_INSTALLED = true;
}

const vm = require('vm');

const readline = require('readline');
const zlib = require('zlib');

const MAX_TDC_SOURCE_BYTES = 2 * 1024 * 1024;
const MAX_EVENTS = 1000;

function PluginArray() { throw new TypeError('Illegal constructor'); }
function Plugin() { throw new TypeError('Illegal constructor'); }

Object.defineProperties(PluginArray.prototype, {
    length: {
        get() {
            let length = 0;
            while (Object.prototype.hasOwnProperty.call(this, length)) length++;
            return length;
        },
    },
    item: { value(index) { return this[index] || null; } },
    namedItem: { value(name) { return this[name] || null; } },
    refresh: { value() {} },
    [Symbol.iterator]: { value: Array.prototype[Symbol.iterator] },
    [Symbol.toStringTag]: { value: 'PluginArray' },
});
Object.defineProperties(Plugin.prototype, {
    length: {
        get() {
            let length = 0;
            while (Object.prototype.hasOwnProperty.call(this, length)) length++;
            return length;
        },
    },
    item: { value(index) { return this[index] || null; } },
    namedItem: { value(name) { return this[name] || null; } },
    [Symbol.iterator]: { value: Array.prototype[Symbol.iterator] },
    [Symbol.toStringTag]: { value: 'Plugin' },
});

function createPluginArray() {
    const definitions = [
        { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    ];
    const plugins = Object.create(PluginArray.prototype);
    definitions.forEach((definition, index) => {
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperties(plugin, {
            name: { value: definition.name, enumerable: true },
            filename: { value: definition.filename, enumerable: true },
            description: { value: definition.description, enumerable: true },
        });
        Object.defineProperty(plugins, index, { value: plugin, enumerable: true });
        Object.defineProperty(plugins, definition.name, { value: plugin });
    });
    return plugins;
}

function updateCanvasSeed(canvas, operation, values) {
    const input = `${operation}:${values.join(':')}`;
    let state = canvas.__drawSeed || 0x811c9dc5;
    for (let index = 0; index < input.length; index++) {
        state ^= input.charCodeAt(index);
        state = Math.imul(state, 0x01000193) >>> 0;
    }
    canvas.__drawSeed = state || 1;
}

function canvasImageData(x, y, width, height, drawSeed = 0) {
    const normalizedWidth = Math.max(0, Math.floor(Number(width) || 0));
    const normalizedHeight = Math.max(0, Math.floor(Number(height) || 0));
    const data = new Uint8ClampedArray(normalizedWidth * normalizedHeight * 4);
    if (!drawSeed || normalizedWidth === 0 || normalizedHeight === 0) {
        return { data, width: normalizedWidth, height: normalizedHeight };
    }
    let state = (
        drawSeed ^ (x | 0) ^ ((y | 0) << 7)
        ^ (normalizedWidth << 14) ^ (normalizedHeight << 21)
    ) >>> 0;
    const pixelCount = normalizedWidth * normalizedHeight;
    const sampleCount = Math.max(1, Math.floor(pixelCount * 0.052));
    for (let index = 0; index < sampleCount; index++) {
        state = (Math.imul(state ^ (state >>> 15), 2246822519) + 3266489917) >>> 0;
        const pixel = state % pixelCount;
        data[pixel * 4 + 3] = 32 + ((state >>> 8) % 224);
    }
    return { data, width: normalizedWidth, height: normalizedHeight };
}

const CRC_TABLE = (() => {
    const table = new Uint32Array(256);
    for (let value = 0; value < 256; value++) {
        let crc = value;
        for (let bit = 0; bit < 8; bit++) {
            crc = (crc & 1) ? (0xedb88320 ^ (crc >>> 1)) : (crc >>> 1);
        }
        table[value] = crc >>> 0;
    }
    return table;
})();

function crc32(buffer) {
    let crc = 0xffffffff;
    for (const value of buffer) crc = CRC_TABLE[(crc ^ value) & 0xff] ^ (crc >>> 8);
    return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data) {
    const name = Buffer.from(type, 'ascii');
    const chunk = Buffer.alloc(12 + data.length);
    chunk.writeUInt32BE(data.length, 0);
    name.copy(chunk, 4);
    data.copy(chunk, 8);
    chunk.writeUInt32BE(crc32(Buffer.concat([name, data])), 8 + data.length);
    return chunk;
}

function canvasDataUrl(canvas) {
    const width = Math.max(1, Math.min(2048, Math.floor(Number(canvas.width) || 300)));
    const height = Math.max(1, Math.min(2048, Math.floor(Number(canvas.height) || 150)));
    const image = canvasImageData(0, 0, width, height, canvas.__drawSeed);
    const scanlines = Buffer.alloc(height * (1 + width * 4));
    for (let row = 0; row < height; row++) {
        const targetOffset = row * (1 + width * 4);
        scanlines[targetOffset] = 0;
        Buffer.from(image.data.buffer, image.data.byteOffset + row * width * 4, width * 4)
            .copy(scanlines, targetOffset + 1);
    }
    const header = Buffer.alloc(13);
    header.writeUInt32BE(width, 0);
    header.writeUInt32BE(height, 4);
    header[8] = 8;
    header[9] = 6;
    const png = Buffer.concat([
        Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
        pngChunk('IHDR', header),
        pngChunk('IDAT', zlib.deflateSync(scanlines, { level: 9 })),
        pngChunk('IEND', Buffer.alloc(0)),
    ]);
    return `data:image/png;base64,${png.toString('base64')}`;
}

function createCanvas2DContext(canvas) {
    const context = {
        canvas, fillStyle: '', strokeStyle: '',
        lineWidth: 1, font: '10px sans-serif', textBaseline: 'alphabetic', textAlign: 'start',
        fillRect(x, y, width, height) {
            updateCanvasSeed(canvas, 'fillRect', [x, y, width, height, context.fillStyle]);
        },
        strokeRect(x, y, width, height) {
            updateCanvasSeed(canvas, 'strokeRect', [x, y, width, height, context.strokeStyle]);
        },
        clearRect: () => {},
        fillText(value, x, y) {
            updateCanvasSeed(canvas, 'fillText', [value, x, y, context.font, context.fillStyle]);
        },
        strokeText(value, x, y) {
            updateCanvasSeed(canvas, 'strokeText', [value, x, y, context.font, context.strokeStyle]);
        },
        beginPath: () => {}, closePath: () => {},
        moveTo: () => {}, lineTo: () => {}, arc: () => {}, rect: () => {},
        fill: () => {}, stroke: () => {}, save: () => {}, restore: () => {},
        translate: () => {}, rotate: () => {}, scale: () => {},
        measureText: (value) => ({ width: String(value).length * 8 }),
        getImageData: (x, y, width, height) => (
            canvasImageData(x, y, width, height, canvas.__drawSeed)
        ),
        putImageData: () => {},
        createImageData: (width, height) => ({
            data: new Uint8ClampedArray(Math.max(0, width * height * 4)), width, height,
        }),
        drawImage: () => {},
    };
    Object.defineProperty(context, Symbol.toStringTag, { value: 'CanvasRenderingContext2D' });
    return context;
}

function createWebGLContext(canvas, fingerprint = {}) {
    const debugRendererInfo = {
        UNMASKED_VENDOR_WEBGL: 0x9245,
        UNMASKED_RENDERER_WEBGL: 0x9246,
    };
    const context = {
        canvas,
        VENDOR: 0x1f00,
        RENDERER: 0x1f01,
        VERSION: 0x1f02,
        SHADING_LANGUAGE_VERSION: 0x8b8c,
        MAX_TEXTURE_SIZE: 0x0d33,
        MAX_VIEWPORT_DIMS: 0x0d3a,
        getExtension(name) {
            return name === 'WEBGL_debug_renderer_info' ? debugRendererInfo : null;
        },
        getSupportedExtensions: () => [
            'ANGLE_instanced_arrays', 'EXT_blend_minmax', 'EXT_color_buffer_half_float',
            'EXT_float_blend', 'EXT_frag_depth', 'EXT_shader_texture_lod',
            'EXT_texture_filter_anisotropic', 'OES_element_index_uint',
            'OES_standard_derivatives', 'OES_texture_float', 'OES_vertex_array_object',
            'WEBGL_debug_renderer_info', 'WEBGL_lose_context',
        ],
        getContextAttributes: () => ({
            alpha: true, antialias: true, depth: true, desynchronized: false,
            failIfMajorPerformanceCaveat: false, powerPreference: 'default',
            premultipliedAlpha: true, preserveDrawingBuffer: false, stencil: false,
            xrCompatible: false,
        }),
        getParameter(parameter) {
            switch (parameter) {
                case 0x9245: return fingerprint.webglVendor || 'Google Inc. (NVIDIA)';
                case 0x9246:
                    return fingerprint.webglRenderer || 'ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                case 0x1f00: return 'WebKit';
                case 0x1f01: return 'WebKit WebGL';
                case 0x1f02: return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
                case 0x8b8c: return 'WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)';
                case 0x0d33: return 16384;
                case 0x0d3a: return new Int32Array([32767, 32767]);
                default: return null;
            }
        },
        getShaderPrecisionFormat: () => ({ rangeMin: 127, rangeMax: 127, precision: 23 }),
        createBuffer: () => ({}), createProgram: () => ({}), createShader: () => ({}),
        bindBuffer: () => {}, bufferData: () => {}, shaderSource: () => {}, compileShader: () => {},
        attachShader: () => {}, linkProgram: () => {}, useProgram: () => {}, drawArrays: () => {},
        viewport: () => {}, clearColor: () => {}, clear: () => {}, readPixels: () => {},
    };
    Object.defineProperty(context, Symbol.toStringTag, { value: 'WebGLRenderingContext' });
    return context;
}

function validateNumber(value, name) {
    if (typeof value !== 'number' || !Number.isFinite(value)) {
        throw new Error(`${name} must be a finite number`);
    }
    return value;
}

function validateParams(params) {
    if (!params || typeof params !== 'object' || Array.isArray(params)) {
        throw new Error('request must be an object');
    }
    if (typeof params.tdcSource !== 'string' || params.tdcSource.length === 0) {
        throw new Error('tdcSource is required');
    }
    if (Buffer.byteLength(params.tdcSource, 'utf8') > MAX_TDC_SOURCE_BYTES) {
        throw new Error('tdcSource is too large');
    }
    if (params.events !== undefined && !Array.isArray(params.events)) {
        throw new Error('events must be an array');
    }
    if (Array.isArray(params.events) && params.events.length > MAX_EVENTS) {
        throw new Error('too many events');
    }
}

function normalizeViewport(params) {
    const viewport = params.viewport || {};
    const width = Number.isFinite(viewport.width) ? Math.round(viewport.width) : 340;
    const height = Number.isFinite(viewport.height) ? Math.round(viewport.height) : 243;
    if (width < 100 || width > 4096 || height < 100 || height > 4096) {
        throw new Error('invalid viewport dimensions');
    }
    return { width, height };
}

function createResources() {
    const timeouts = new Set();
    const intervals = new Set();
    return {
        setTimeout(fn, delay, ...args) {
            const id = setTimeout(() => {
                timeouts.delete(id);
                try { fn(...args); } catch (_) {}
            }, delay);
            timeouts.add(id);
            return id;
        },
        clearTimeout(id) {
            timeouts.delete(id);
            clearTimeout(id);
        },
        setInterval(fn, delay, ...args) {
            const id = setInterval(() => {
                try { fn(...args); } catch (_) {}
            }, delay);
            intervals.add(id);
            return id;
        },
        clearInterval(id) {
            intervals.delete(id);
            clearInterval(id);
        },
        cleanup() {
            for (const id of timeouts) clearTimeout(id);
            for (const id of intervals) clearInterval(id);
            timeouts.clear();
            intervals.clear();
        },
    };
}

function makeEventFactory(startTime) {
    return function makeEvent(type, props = {}) {
        return Object.assign({
            type,
            preventDefault: () => {},
            stopPropagation: () => {},
            stopImmediatePropagation: () => {},
            target: {},
            currentTarget: {},
            timeStamp: 0,
            isTrusted: true,
            bubbles: true,
            cancelable: true,
            defaultPrevented: false,
        }, props);
    };
}

function dispatch(listeners, type, event) {
    const handlers = listeners[type] || [];
    for (const handler of handlers.slice()) {
        try { handler(event); } catch (_) {}
    }
}

function makeFakeElement(viewport, fingerprint = {}) {
    const listeners = {};
    const element = {
        style: {}, children: [], childNodes: [], attributes: {},
        innerHTML: '', innerText: '', textContent: '', className: '', id: '',
        tagName: 'DIV', parentNode: null, parentElement: null, ownerDocument: null,
        width: 300, height: 150, __drawSeed: Number(fingerprint.canvasSeed) || 0x811c9dc5,
        clientWidth: viewport.width, clientHeight: viewport.height,
        offsetWidth: viewport.width, offsetHeight: viewport.height,
        offsetLeft: 0, offsetTop: 0,
        scrollWidth: viewport.width, scrollHeight: viewport.height,
        getContext(type) {
            const contextType = String(type || '2d').toLowerCase();
            if (contextType === 'webgl' || contextType === 'experimental-webgl') {
                return createWebGLContext(this, fingerprint);
            }
            return contextType === '2d' ? createCanvas2DContext(this) : null;
        },
        toDataURL(type) {
            return canvasDataUrl(this);
        },
        appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
        removeChild(child) { return child; },
        insertBefore(child) { this.children.push(child); return child; },
        addEventListener(type, handler) {
            (listeners[type] = listeners[type] || []).push(handler);
        },
        removeEventListener(type, handler) {
            if (listeners[type]) listeners[type] = listeners[type].filter((item) => item !== handler);
        },
        dispatchEvent(event) { dispatch(listeners, event.type, event); return true; },
        getElementsByTagName: () => [], getElementsByClassName: () => [],
        querySelector: () => null, querySelectorAll: () => [],
        getAttribute(name) { return this.attributes[name] ?? null; },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        removeAttribute(name) { delete this.attributes[name]; },
        getBoundingClientRect: () => ({
            top: 0, left: 0, right: viewport.width, bottom: viewport.height,
            width: viewport.width, height: viewport.height,
        }),
        cloneNode() { return makeFakeElement(viewport); },
    };
    // keep canvas seed from fingerprint; do not force 0
    Object.defineProperty(element, Symbol.toStringTag, {
        value: 'HTMLElement', configurable: true,
    });
    return element;
}

function buildGlobals(params, resources) {
    // Virtual clock advances with the synthetic event timeline.
    // Real TDC samples Date.now()/performance.now() while collecting; if events
    // span 1-2s but host time only moves a few ms, continuous solves look fake.
    const startTime = Date.now();
    let virtualNow = startTime;
    const advanceClock = (ms) => {
        const delta = Number(ms) || 0;
        if (delta > 0) virtualNow = Math.max(virtualNow, startTime + delta);
    };
    const viewport = normalizeViewport(params);
    const makeEvent = makeEventFactory(startTime);
    const eventListeners = {};
    const docListeners = {};
    const userAgent = params.userAgent || (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
        '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'
    );
    const fingerprint = params.fingerprint && typeof params.fingerprint === 'object'
        ? params.fingerprint : {};
    const fpWindow = fingerprint.window || {};
    const fpScreen = fingerprint.screen || {};
    const chromeMajor = Number(fingerprint.chromeMajor) || 146;
    const useViewportAsWindow = params.useViewportAsWindow === true;

    const window = {
        innerWidth: useViewportAsWindow ? viewport.width : (Number(fpWindow.innerWidth) || 1440),
        innerHeight: useViewportAsWindow ? viewport.height : (Number(fpWindow.innerHeight) || 900),
        outerWidth: Number(fpWindow.outerWidth) || 1456,
        outerHeight: Number(fpWindow.outerHeight) || 988,
        devicePixelRatio: Number(fpWindow.devicePixelRatio) || 1,
        screenX: 0, screenY: 0, scrollX: 0, scrollY: 0,
        pageXOffset: 0, pageYOffset: 0, name: '', status: '', closed: false,
        frameElement: null, length: 0,
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        indexedDB: {},
        chrome: {
            app: {
                isInstalled: false,
                InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
                getDetails() { return null; },
                getIsInstalled() { return false; },
            },
            runtime: {
                id: undefined,
                connect() { return { onMessage: { addListener() {} }, postMessage() {}, disconnect() {} }; },
                sendMessage() {},
            },
            csi() { return { startE: virtualNow - 400, onloadT: virtualNow - 80, pageT: 320, tran: 15 }; },
            loadTimes() {
                const now = virtualNow / 1000;
                return {
                    requestTime: now - 0.4, startLoadTime: now - 0.4, commitLoadTime: now - 0.28,
                    finishDocumentLoadTime: now - 0.12, finishLoadTime: now - 0.08,
                    firstPaintTime: now - 0.15, firstPaintAfterLoadTime: 0, navigationType: 'Other',
                    wasFetchedViaSpdy: true, wasNpnNegotiated: true, npnNegotiatedProtocol: 'h2',
                    wasAlternateProtocolAvailable: false, connectionInfo: 'h2',
                };
            },
        },
        performance: {
            timing: {
                navigationStart: startTime, fetchStart: startTime + 1,
                domainLookupStart: startTime + 2, domainLookupEnd: startTime + 3,
                connectStart: startTime + 4, connectEnd: startTime + 15,
                requestStart: startTime + 20, responseStart: startTime + 50,
                responseEnd: startTime + 80, domLoading: startTime + 90,
                domInteractive: startTime + 200, domContentLoadedEventStart: startTime + 210,
                domContentLoadedEventEnd: startTime + 220, domComplete: startTime + 300,
                loadEventStart: startTime + 310, loadEventEnd: startTime + 320,
            },
            now: () => Math.max(0, virtualNow - startTime),
            timeOrigin: startTime,
        },
        addEventListener(type, handler) {
            (eventListeners[type] = eventListeners[type] || []).push(handler);
        },
        removeEventListener(type, handler) {
            if (eventListeners[type]) {
                eventListeners[type] = eventListeners[type].filter((item) => item !== handler);
            }
        },
        dispatchEvent(event) { dispatch(eventListeners, event.type, event); return true; },
        setTimeout: resources.setTimeout,
        clearTimeout: resources.clearTimeout,
        setInterval: resources.setInterval,
        clearInterval: resources.clearInterval,
        requestAnimationFrame: (fn) => resources.setTimeout(fn, 16),
        cancelAnimationFrame: resources.clearTimeout,
        getComputedStyle: () => ({ getPropertyValue: () => '', getPropertyPriority: () => '', item: () => '', length: 0 }),
        matchMedia: (query) => {
            const media = String(query || '');
            const matches = media.includes('pointer: fine') || media.includes('hover: hover') || media.includes('min-width') || media.includes('prefers-color-scheme') || media.includes('screen');
            return {
                media, matches, onchange: null,
                addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; },
            };
        },
        postMessage: () => {}, alert: () => {}, confirm: () => true, prompt: () => null,
        open: () => null, close: () => {}, focus: () => {}, blur: () => {},
        speechSynthesis: { getVoices() { return []; }, speaking: false, pending: false, paused: false, onvoiceschanged: null, cancel() {}, pause() {}, resume() {}, speak() {} },
        isSecureContext: true,
        origin: 'https://turing.captcha.gtimg.com',
    };
    // Captcha runs in iframe: top/self must differ.
    const parentInnerW = Number(fpWindow.innerWidth) || 1440;
    const parentInnerH = Number(fpWindow.innerHeight) || 900;
    const parentOuterW = Number(fpWindow.outerWidth) || 1456;
    const parentOuterH = Number(fpWindow.outerHeight) || 988;
    const parentHref = params.documentReferrer || 'https://cloud.tencent.com/product/captcha';
    let parentProtocol = 'https:';
    let parentHost = 'cloud.tencent.com';
    let parentHostname = 'cloud.tencent.com';
    let parentPort = '';
    let parentPathname = '/product/captcha';
    let parentSearch = '';
    let parentHash = '';
    let parentOrigin = 'https://cloud.tencent.com';
    try {
        const parentUrl = new URL(parentHref);
        parentProtocol = parentUrl.protocol || parentProtocol;
        parentHost = parentUrl.host || parentHost;
        parentHostname = parentUrl.hostname || parentHostname;
        parentPort = parentUrl.port || '';
        parentPathname = parentUrl.pathname || parentPathname;
        parentSearch = parentUrl.search || '';
        parentHash = parentUrl.hash || '';
        parentOrigin = parentUrl.origin || parentOrigin;
    } catch (error) {
        // keep product defaults
    }
    const topWindow = {
        innerWidth: parentInnerW,
        innerHeight: parentInnerH,
        outerWidth: parentOuterW,
        outerHeight: parentOuterH,
        devicePixelRatio: Number(fpWindow.devicePixelRatio) || 1,
        screenX: 0, screenY: 0, scrollX: 0, scrollY: 0,
        pageXOffset: 0, pageYOffset: 0, name: '', status: '', closed: false, length: 1,
        origin: parentOrigin,
        location: {
            href: parentHref,
            protocol: parentProtocol, host: parentHost, hostname: parentHostname,
            port: parentPort, pathname: parentPathname, search: parentSearch, hash: parentHash,
            origin: parentOrigin,
        },
        document: {
            URL: parentHref,
            documentURI: parentHref,
            referrer: parentOrigin + '/',
            domain: parentHostname,
            hidden: false, visibilityState: 'visible', readyState: 'complete',
            hasFocus: () => true,
            cookie: '',
        },
        navigator: null,
        chrome: null,
        performance: null,
        addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
        getComputedStyle: () => ({ getPropertyValue: () => '' }),
        matchMedia: (query) => ({ media: String(query || ''), matches: true, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }),
        postMessage() {}, focus() {}, blur() {},
    };
    topWindow.window = topWindow;
    topWindow.self = topWindow;
    topWindow.top = topWindow;
    topWindow.parent = topWindow;
    topWindow.frames = topWindow;

    window.window = window;
    window.self = window;
    window.globalThis = window;
    window.top = topWindow;
    window.parent = topWindow;
    window.frames = window;
    window.opener = null;
    window.name = 'tcaptcha_iframe';
    window.frameElement = {
        tagName: 'IFRAME',
        nodeName: 'IFRAME',
        id: 'tcaptcha_iframe_dy',
        name: 'tcaptcha_iframe',
        src: 'https://turing.captcha.gtimg.com/1/template/drag_ele.html',
        contentWindow: window,
        width: String(viewport.width),
        height: String(viewport.height),
        getBoundingClientRect() {
            return {
                x: 240, y: 120, left: 240, top: 120,
                right: 240 + viewport.width, bottom: 120 + viewport.height,
                width: viewport.width, height: viewport.height,
            };
        },
        getAttribute(name) {
            if (name === 'width') return String(viewport.width);
            if (name === 'height') return String(viewport.height);
            return null;
        },
        style: {},
    };

    const navigator = {
        userAgent,
        appVersion: userAgent.replace(/^Mozilla\//, ''),
        appName: 'Netscape', appCodeName: 'Mozilla', platform: fingerprint.platform || 'Win32',
        product: 'Gecko', productSub: '20030107', vendor: 'Google Inc.', vendorSub: '',
        language: fingerprint.language || 'zh-CN',
        languages: Array.isArray(fingerprint.languages) && fingerprint.languages.length ? fingerprint.languages.slice() : ['zh-CN'],
        hardwareConcurrency: Number(fingerprint.hardwareConcurrency) || 8,
        deviceMemory: Number(fingerprint.deviceMemory) || 8,
        maxTouchPoints: Number(fingerprint.maxTouchPoints) || 0,
        onLine: true, cookieEnabled: true, doNotTrack: null, pdfViewerEnabled: true, webdriver: false,
        plugins: createPluginArray(),
        mimeTypes: {
            0: { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
            1: { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
            length: 2, item(index) { return this[index] || null; },
            namedItem(name) { return this[0].type === name ? this[0] : (this[1].type === name ? this[1] : null); },
        },
        userAgentData: {
            brands: [
                { brand: 'Chromium', version: String(chromeMajor) },
                { brand: 'Not-A.Brand', version: '24' },
                { brand: 'Google Chrome', version: String(chromeMajor) },
            ],
            mobile: false, platform: 'Windows',
            getHighEntropyValues: async (hints) => {
                // Prefer the real product build when UA carries it (e.g. 146.0.7680.177).
                let full = `${chromeMajor}.0.7680.177`;
                try {
                    const m = String(userAgent || '').match(/Chrome\/(\d+(?:\.\d+){1,3})/);
                    if (m && m[1]) full = m[1];
                } catch (_) {}
                const values = {
                    architecture: 'x86', bitness: '64', model: '', platform: 'Windows',
                    platformVersion: '15.0.0', uaFullVersion: full,
                    fullVersionList: [
                        { brand: 'Chromium', version: full },
                        { brand: 'Not-A.Brand', version: '10.0.2.3' },
                        { brand: 'Google Chrome', version: full },
                    ],
                };
                return Object.fromEntries((hints || []).filter((key) => key in values).map((key) => [key, values[key]]));
            },
        },
        connection: { effectiveType: '4g', rtt: 50, downlink: 10, saveData: false },
        mediaDevices: {
            enumerateDevices: async () => ([
                { deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default' },
                { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' },
                { deviceId: 'default', kind: 'videoinput', label: '', groupId: 'default' },
            ]),
            getUserMedia: async () => { throw Object.assign(new Error('Permission denied'), { name: 'NotAllowedError' }); },
        },
        permissions: { query: async () => ({ state: 'prompt', onchange: null }) },
        javaEnabled: () => false,
        sendBeacon: () => true,
        geolocation: { getCurrentPosition() {}, watchPosition() { return 0; }, clearWatch() {} },
        getBattery: async () => ({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1, addEventListener() {}, removeEventListener() {} }),
        getGamepads: () => [null, null, null, null],
    };

    if (typeof topWindow !== 'undefined') {
        topWindow.navigator = navigator;
        topWindow.chrome = window.chrome;
        topWindow.performance = window.performance;
        topWindow.matchMedia = window.matchMedia;
    }
    window.navigator = navigator;
    window.clientInformation = navigator;

    const documentElement = makeFakeElement(viewport, fingerprint);
    documentElement.tagName = 'HTML';
    documentElement.clientWidth = window.innerWidth;
    documentElement.clientHeight = window.innerHeight;
    const body = makeFakeElement(viewport, fingerprint); body.tagName = 'BODY';
    const head = makeFakeElement(viewport, fingerprint); head.tagName = 'HEAD';
    const document = {
        documentElement, body, head,
        cookie: '', title: '',
        URL: 'https://turing.captcha.gtimg.com/1/template/drag_ele.html',
        referrer: params.documentReferrer || 'https://cloud.tencent.com/',
        domain: 'turing.captcha.gtimg.com',
        readyState: 'complete', visibilityState: 'visible', hidden: false,
        hasFocus: () => true,
        createElement(tag) {
            const element = makeFakeElement(viewport, fingerprint);
            element.tagName = String(tag).toUpperCase();
            Object.defineProperty(element, Symbol.toStringTag, {
                value: element.tagName === 'CANVAS' ? 'HTMLCanvasElement' : 'HTMLElement',
            });
            return element;
        },
        createElementNS(ns, tag) { return this.createElement(tag); },
        createTextNode: (text) => ({ nodeType: 3, textContent: text }),
        createDocumentFragment: () => makeFakeElement(viewport, fingerprint),
        getElementById: () => null,
        getElementsByTagName: () => [], getElementsByClassName: () => [], getElementsByName: () => [],
        querySelector: () => null, querySelectorAll: () => [],
        addEventListener(type, handler) {
            (docListeners[type] = docListeners[type] || []).push(handler);
        },
        removeEventListener(type, handler) {
            if (docListeners[type]) {
                docListeners[type] = docListeners[type].filter((item) => item !== handler);
            }
        },
        dispatchEvent(event) { dispatch(docListeners, event.type, event); return true; },
        write: () => {}, writeln: () => {},
    };
    document.defaultView = window;
    documentElement.ownerDocument = document;
    body.ownerDocument = document;
    head.ownerDocument = document;
    window.document = document;

    const screen = {
        width: Number(fpScreen.width) || 1920, height: Number(fpScreen.height) || 1080,
        availWidth: Number(fpScreen.availWidth) || Number(fpScreen.width) || 1920,
        availHeight: Number(fpScreen.availHeight) || 1040,
        colorDepth: Number(fpScreen.colorDepth) || 24, pixelDepth: Number(fpScreen.pixelDepth) || 24,
        availLeft: 0, availTop: 0,
        orientation: { type: 'landscape-primary', angle: 0 },
    };
    const location = {
        href: 'https://turing.captcha.gtimg.com/1/template/drag_ele.html',
        protocol: 'https:', host: 'turing.captcha.gtimg.com',
        hostname: 'turing.captcha.gtimg.com', port: '',
        pathname: '/1/template/drag_ele.html', search: '', hash: '',
        origin: 'https://turing.captcha.gtimg.com',
    };
    const Event = function Event(type, options = {}) { return makeEvent(type, options); };
    const MouseEvent = function MouseEvent(type, options = {}) { return makeEvent(type, options); };
    const PointerEvent = function PointerEvent(type, options = {}) { return makeEvent(type, { pointerType: 'mouse', isPrimary: true, pointerId: 1, ...options }); };

    window.Event = Event;
    window.MouseEvent = MouseEvent;
    window.PointerEvent = PointerEvent;
    const globals = {
        window, self: window, globalThis: window, navigator, document, screen, location,
        Event, MouseEvent, PointerEvent, PluginArray, Plugin,
        history: {
            length: 1, state: null, pushState: () => {}, replaceState: () => {},
            go: () => {}, back: () => {}, forward: () => {},
        },
        parent: window.parent, top: window.top, frames: window, opener: null, closed: false, frameElement: window.frameElement,
        innerWidth: window.innerWidth, innerHeight: window.innerHeight,
        outerWidth: window.outerWidth, outerHeight: window.outerHeight,
        devicePixelRatio: window.devicePixelRatio, screenX: 0, screenY: 0, pageXOffset: 0, pageYOffset: 0,
        scrollX: 0, scrollY: 0,
        XMLHttpRequest: function XMLHttpRequest() {
            return {
                open: () => {}, send: () => {}, setRequestHeader: () => {},
                onload: null, onerror: null, readyState: 0, status: 0, responseText: '',
            };
        },
        fetch: () => Promise.resolve({
            ok: true, json: () => Promise.resolve({}), text: () => Promise.resolve(''),
        }),
        WebSocket: function WebSocket() {}, Worker: function Worker() {},
        MutationObserver: function MutationObserver() { return { observe: () => {}, disconnect: () => {} }; },
        IntersectionObserver: function IntersectionObserver() { return { observe: () => {}, disconnect: () => {} }; },
        ResizeObserver: function ResizeObserver() { return { observe: () => {}, disconnect: () => {} }; },
        PerformanceObserver: function PerformanceObserver() { return { observe: () => {}, disconnect: () => {} }; },
        RTCPeerConnection: function RTCPeerConnection() {
            return {
                createDataChannel: () => ({}),
                createOffer: () => Promise.resolve({ type: 'offer', sdp: '' }),
                setLocalDescription: () => Promise.resolve(), onicecandidate: null, close: () => {},
            };
        },
        webkitRTCPeerConnection: function webkitRTCPeerConnection() {},
        mozRTCPeerConnection: function mozRTCPeerConnection() {},
        msRTCPeerConnection: function msRTCPeerConnection() {},
        ArrayBuffer, Uint8Array, Uint8ClampedArray, Int8Array,
        Uint16Array, Int16Array, Uint32Array, Int32Array,
        Float32Array, Float64Array, DataView,
        Math, Date, JSON, parseInt, parseFloat, isNaN, isFinite,
        String, Number, Boolean, Array, Object, RegExp, Error, Function,
        Promise, Map, Set, WeakMap, WeakSet,
        encodeURI, encodeURIComponent, decodeURI, decodeURIComponent, escape, unescape,
        btoa: (value) => Buffer.from(value, 'binary').toString('base64'),
        atob: (value) => Buffer.from(value, 'base64').toString('binary'),
        setTimeout: resources.setTimeout,
        clearTimeout: resources.clearTimeout,
        setInterval: resources.setInterval,
        clearInterval: resources.clearInterval,
        // setImmediate omitted
        requestAnimationFrame: (fn) => resources.setTimeout(fn, 16),
        cancelAnimationFrame: resources.clearTimeout,
        console: { log: () => {}, warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
    };
    // Hide Node host leaks from captcha sandbox.
    globals.Buffer = undefined;
    globals.global = undefined;
    globals.process = undefined;
    globals.require = undefined;
    globals.module = undefined;
    globals.exports = undefined;
    globals.__dirname = undefined;
    globals.__filename = undefined;
    globals.setImmediate = undefined;
    globals.clearImmediate = undefined;
    globals.SharedArrayBuffer = undefined;
    globals.globalThis = window;
    for (const [key, value] of Object.entries(globals)) {
        if (!(key in window)) window[key] = value;
    }
    Object.defineProperty(navigator, 'webdriver', { get() { return false; }, configurable: true });

    // Function-style Date shim: keep constructor name/prototype native-like.
    const RealDate = Date;
    const SandboxDate = function Date(...args) {
        if (new.target) {
            if (args.length === 0) return new RealDate(virtualNow);
            return new RealDate(...args);
        }
        // bare Date() returns a string in browsers
        return RealDate();
    };
    SandboxDate.prototype = RealDate.prototype;
    Object.setPrototypeOf(SandboxDate, RealDate);
    SandboxDate.now = () => virtualNow;
    SandboxDate.parse = RealDate.parse.bind(RealDate);
    SandboxDate.UTC = RealDate.UTC.bind(RealDate);
    Object.defineProperty(SandboxDate, 'name', { value: 'Date' });
    Object.defineProperty(SandboxDate, 'length', { value: RealDate.length });
    globals.Date = SandboxDate;
    window.Date = SandboxDate;

    installNativeToString(window, [
        'alert', 'confirm', 'prompt', 'setTimeout', 'clearTimeout',
        'setInterval', 'clearInterval', 'requestAnimationFrame', 'cancelAnimationFrame',
        'getComputedStyle', 'matchMedia', 'postMessage', 'btoa', 'atob', 'open', 'close', 'focus', 'blur',
    ]);
    installNativeToString(navigator, [
        'sendBeacon', 'javaEnabled', 'getBattery', 'getGamepads',
    ]);
    installNativeToString(document, [
        'createElement', 'getElementsByTagName', 'getElementsByClassName',
        'querySelector', 'querySelectorAll', 'addEventListener', 'removeEventListener',
        'dispatchEvent', 'hasFocus',
    ]);
    ensureStackSanitizeInstalled();

    return {
        globals, window, document, eventListeners, docListeners, makeEvent, viewport,
        advanceClock, startTime, getVirtualNow: () => virtualNow,
    };
}

function normalizeEvents(params) {
    if (Array.isArray(params.events)) {
        let previousTime = -1;
        return params.events.map((raw, index) => {
            if (!raw || typeof raw !== 'object') throw new Error(`events[${index}] must be an object`);
            if (!['mousemove', 'mousedown', 'mouseup', 'click'].includes(raw.type)) {
                throw new Error(`events[${index}] has unsupported type`);
            }
            const event = {
                type: raw.type,
                x: validateNumber(raw.x, `events[${index}].x`),
                y: validateNumber(raw.y, `events[${index}].y`),
                time: validateNumber(raw.time, `events[${index}].time`),
                button: Number.isInteger(raw.button) ? raw.button : 0,
                buttons: Number.isInteger(raw.buttons) ? raw.buttons : 0,
            };
            if (event.time < previousTime) throw new Error('event timestamps must be monotonic');
            previousTime = event.time;
            return event;
        });
    }

    const legacy = [];
    for (const point of params.trajectory || []) {
        if (!Array.isArray(point) || point.length < 3) throw new Error('invalid legacy trajectory point');
        legacy.push({ type: 'mousemove', x: point[0], y: point[1], time: point[2], button: 0, buttons: 0 });
    }
    let time = legacy.length ? legacy[legacy.length - 1].time : 0;
    for (const point of params.clicks || []) {
        if (!Array.isArray(point) || point.length < 2) throw new Error('invalid legacy click point');
        legacy.push({ type: 'mousedown', x: point[0], y: point[1], time: ++time, button: 0, buttons: 1 });
        legacy.push({ type: 'mouseup', x: point[0], y: point[1], time: ++time, button: 0, buttons: 0 });
        legacy.push({ type: 'click', x: point[0], y: point[1], time: ++time, button: 0, buttons: 0 });
    }
    return legacy;
}

function dispatchInputEvent(state, raw) {
    if (typeof state.advanceClock === 'function' && raw && raw.time != null) {
        state.advanceClock(raw.time);
    }
    if (!state._pointer) state._pointer = { x: raw.x, y: raw.y };
    const prev = state._pointer;
    const movementX = Math.round(raw.x - prev.x);
    const movementY = Math.round(raw.y - prev.y);
    state._pointer = { x: raw.x, y: raw.y };
    const screenX = Math.round((Number(state.window.screenX) || 0) + raw.x);
    const screenY = Math.round((Number(state.window.screenY) || 0) + raw.y + 85);
    const target = (state.document && (state.document.body || state.document.documentElement)) || {};
    const event = state.makeEvent(raw.type, {
        clientX: raw.x, clientY: raw.y, pageX: raw.x, pageY: raw.y,
        screenX, screenY, offsetX: raw.x, offsetY: raw.y,
        layerX: raw.x, layerY: raw.y, movementX, movementY,
        x: raw.x, y: raw.y, button: raw.button || 0, buttons: raw.buttons || 0,
        which: (raw.button || 0) === 0 ? 1 : (raw.button || 0),
        detail: raw.type === 'click' ? 1 : 0,
        timeStamp: raw.time,
        target, currentTarget: target, srcElement: target,
        view: state.window, pointerType: 'mouse', isPrimary: true,
        pressure: raw.type === 'mousedown' || (raw.buttons || 0) > 0 ? 0.5 : 0,
    });
    dispatch(state.docListeners, raw.type, event);
    dispatch(state.eventListeners, raw.type, event);
    if (target && typeof target.dispatchEvent === 'function') target.dispatchEvent(event);
}

function processRequest(params) {
    validateParams(params);
    const resources = createResources();
    const state = buildGlobals(params, resources);
    try {
        const context = vm.createContext(Object.assign({}, state.globals, {
            window: state.window, globalThis: state.window, self: state.window,
            document: state.globals.document, navigator: state.globals.navigator,
            chrome: state.window.chrome, performance: state.window.performance,
        }));
        // Prevent host process escape via Function constructor.
        const SandboxFunction = function Function(...args) {
            const body = String(args.length ? args[args.length - 1] : '');
            const names = args.slice(0, Math.max(0, args.length - 1)).map((value, index) => {
                const name = String(value || (`arg${index}`));
                return /^[A-Za-z_$][\w$]*$/.test(name) ? name : `arg${index}`;
            });
            const source = `(function(${names.join(',')}){\n${body}\n})`;
            return vm.runInContext(source, context, { filename: 'sandbox-function.js', timeout: 1000 });
        };
        Object.defineProperty(SandboxFunction, 'name', { value: 'Function' });
        try {
            Object.defineProperty(SandboxFunction, 'prototype', {
                value: Function.prototype,
                writable: false,
                configurable: false,
            });
        } catch (_) {}
        context.Function = SandboxFunction;
        context.eval = function sandboxEval(code) {
            return vm.runInContext(String(code), context, { filename: 'sandbox-eval.js', timeout: 1000 });
        };
        state.window.Function = SandboxFunction;
        state.window.eval = context.eval;
        if (state.globals) {
            state.globals.Function = SandboxFunction;
            state.globals.eval = context.eval;
        }
        NATIVE_TO_STRING_MAP.set(SandboxFunction, 'function Function() { [native code] }');
        NATIVE_TO_STRING_MAP.set(context.eval, 'function eval() { [native code] }');
        const requestedTimeout = Number.isInteger(params.vmTimeoutMs) ? params.vmTimeoutMs : 2000;
        const vmTimeout = Math.max(100, Math.min(5000, requestedTimeout));
        vm.runInContext(params.tdcSource, context, { filename: 'tdc.js', timeout: vmTimeout });

        if (!state.window.TDC || typeof state.window.TDC.getData !== 'function') {
            throw new Error('TDC not defined');
        }
        if (typeof state.advanceClock === 'function') state.advanceClock(20);
        dispatch(state.eventListeners, 'DOMContentLoaded', state.makeEvent('DOMContentLoaded'));
        dispatch(state.docListeners, 'DOMContentLoaded', state.makeEvent('DOMContentLoaded'));
        dispatch(state.eventListeners, 'load', state.makeEvent('load'));
        dispatch(state.docListeners, 'load', state.makeEvent('load'));
        for (const event of normalizeEvents(params)) dispatchInputEvent(state, event);
        if (typeof state.advanceClock === 'function') {
            const events = Array.isArray(params.events) ? params.events : [];
            const last = events.length ? events[events.length - 1] : null;
            const lastTime = last && last.time != null ? Number(last.time) : 0;
            state.advanceClock(lastTime + 35);
        }
        if (params.ft && typeof state.window.TDC.setData === 'function') {
            state.window.TDC.setData({ ft: params.ft });
        }

        const collect = decodeURIComponent(state.window.TDC.getData(true));
        const info = typeof state.window.TDC.getInfo === 'function' ? state.window.TDC.getInfo() : null;
        const eks = info ? info.info : null;
        const tokenid = info ? info.tokenid : null;
        return {
            requestId: params.requestId,
            success: true,
            collect,
            collect_len: collect.length,
            eks,
            tokenid,
        };
    } finally {
        resources.cleanup();
    }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (line) => {
    let requestId = null;
    try {
        const params = JSON.parse(line);
        requestId = params && params.requestId;
        process.stdout.write(`${JSON.stringify(processRequest(params))}\n`);
    } catch (error) {
        process.stdout.write(`${JSON.stringify({
            requestId,
            success: false,
            error: error instanceof Error ? error.message : String(error),
        })}\n`);
    }
});
rl.on('close', () => process.exit(0));

process.stdout.write(`${JSON.stringify({ ready: true })}\n`);
