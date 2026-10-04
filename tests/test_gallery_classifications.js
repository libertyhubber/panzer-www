const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { test } = require('node:test')
const vm = require('node:vm')
const { workerClass, workerSource } = require('./search_worker_harness')

function searchContext() {
    const context = {}
    vm.runInNewContext(workerSource.slice(workerSource.indexOf('function normalizeSearch('),
        workerSource.indexOf('function classificationTags(')), context)
    const compile = context.compileSearch
    context.compileSearch = query => {
        const matcher = compile(query)
        return fields => matcher((Array.isArray(fields) ? fields : [fields]).filter(Boolean).map(context.normalizeSearch))
    }
    return context
}

const appSource = readFileSync(`${__dirname}/../assets/app.js`, 'utf8')
const flush = () => new Promise(resolve => setImmediate(resolve))

function gallery(extraArchive = false, tagGroups = [], customEntries = null, hostname = 'localhost', options = {}) {
    let resolveClassifications, rejectClassifications
    const classifications = new Promise((resolve, reject) => {
        resolveClassifications = resolve
        rejectClassifications = reject
    })
    let html = '', thumbnails = [], classificationNodes = []
    const classificationPattern = /<div class="thumbnail-classification" data-image-id="([^"]+)"[^>]*>(?:<div class="classification-tags"[^>]*>[\s\S]*?<\/div>)?<\/div>/g
    const thumbnailPattern = /<a href="[^"]*" class="thumbnail" style="[^"]*"[^>]*>/g
    const node = {
        clientWidth: options.clientWidth || 456,
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = value },
        style: { setProperty(name, value) { this[name] = value } },
        querySelectorAll: selector => selector === '.thumbnail' ? thumbnails
            : selector === '.classification-tags' ? options.classificationDOM
                ? classificationNodes.map(node => node.group).filter(Boolean) : tagGroups
            : selector === '.thumbnail-classification[data-image-id]' ? classificationNodes
            : selector === '.classification-more' ? options.moreButtons || [] : [],
        get innerHTML() {
            let i = 0
            const result = html.replace(thumbnailPattern, anchor => {
                const style = Object.entries(thumbnails[i++].style).map(([key, value]) =>
                    `${key.replace(/[A-Z]/g, char => '-' + char.toLowerCase())}: ${value};`).join(' ')
                return anchor.replace(/style="[^"]*"/, `style="${style}"`)
            })
            let classificationIndex = 0
            return options.classificationDOM ? result.replace(classificationPattern,
                () => classificationNodes[classificationIndex++].outerHTML) : result
        },
        set innerHTML(value) {
            html = value
            thumbnails = [...value.matchAll(thumbnailPattern)].map(([anchor]) => {
                const attrs = Object.fromEntries([...anchor.matchAll(/([-\w]+)="([^"]*)"/g)]
                    .map(([, name, value]) => [name, value]))
                const style = Object.fromEntries(attrs.style.split(';').filter(part => part.trim()).map(part => {
                    const colon = part.indexOf(':')
                    return [part.slice(0, colon).trim().replace(/-([a-z])/g, (_, char) => char.toUpperCase()),
                        part.slice(colon + 1).trim()]
                }))
                return { style, getAttribute: name => attrs[name] }
            })
            if (options.classificationDOM) {
                classificationNodes = [...value.matchAll(classificationPattern)].map(([markup, imageId]) =>
                    classificationElement(markup, imageId, parseFloat(node.style['--thumbnail-display-size']),
                        options.tagMeasurements))
            }
        },
    }
    const controls = Object.fromEntries(['filter-reactions', 'filter-template', 'filter-search', 'filter-status',
        'filter-search-clear', 'filter-search-icon'].map(id =>
        [id, {
            value: id === 'filter-reactions' ? '0' : '', innerHTML: '',
            get options() {
                return [...this.innerHTML.matchAll(/value="([^"]*)"/g)].map(match => ({
                    value: match[1].replace(/&(amp|quot|lt|gt|#39);/g, entity => ({
                        '&amp;': '&', '&quot;': '"', '&lt;': '<', '&gt;': '>', '&#39;': "'",
                    })[entity]),
                }))
            },
            insertAdjacentHTML(position, html) { this.innerHTML += html },
            focus() { this.focused = true },
            addEventListener(event, handler) { this.handler = handler },
        }]))
    const entries = customEntries || ['2024-01-01_a.jpg', '2024-01-02_b.jpg'].map(name => ({
        name, w: 500, h: 400, x: 0, y: 0,
    }))
    const listeners = {}
    const lightboxListeners = {}
    const lightbox = {
        options: {},
        on(event, handler) { (lightboxListeners[event] ||= []).push(handler) },
        emit(event) { return Promise.all((lightboxListeners[event] || []).map(handler => handler())) },
        loadAndOpen(index) {
            this.openedIndex = index
            const events = {}
            this.pswp = {
                currIndex: index, options: { dataSource: this.options.dataSource },
                opener: { isOpen: true },
                on(event, handler) { (events[event] ||= []).push(handler) },
                emit(event) { for (const handler of events[event] || []) handler() },
                destroy: () => {
                    this.emit('close')
                    this.pswp = undefined
                    for (const handler of events.destroy || []) handler()
                },
            }
            this.emit('change')
            return true
        },
        async close() { await this.emit('close'); this.pswp = undefined },
        async goTo(index) { this.pswp.currIndex = index; await this.emit('change') },
    }
    const initialURL = `http://${hostname}/${options.query || ''}`
    const location = { protocol: 'http:', host: hostname, hostname, href: initialURL }
    const historyEntries = [initialURL]
    const historyStates = [null]
    let historyIndex = 0
    const history = {
        get state() { return historyStates[historyIndex] },
        pushState(state, title, url) {
            historyEntries.splice(++historyIndex)
            historyStates.splice(historyIndex)
            historyEntries.push(url)
            historyStates.push(state)
            location.href = url
        },
        replaceState(state, title, url) {
            historyStates[historyIndex] = state
            historyEntries[historyIndex] = location.href = url
        },
        async go(delta) {
            historyIndex = Math.max(0, Math.min(historyEntries.length - 1, historyIndex + delta))
            location.href = historyEntries[historyIndex]
            for (const handler of listeners.popstate || []) handler()
            await flush()
        },
        entries: historyEntries,
    }
    const requests = []
    const imageRequests = []
    let dialog, overlay
    const context = {
        document: {
            body: { appendChild(node) {
                if (node.id === 'all-tags-overlay') overlay = node
                else dialog = node
            } },
            createElement() {
                const pre = { textContent: '' }
                const firstButton = { focus(options) { this.focused = true; this.focusOptions = options } }
                return {
                    style: {}, attributes: {},
                    setAttribute(name, value) { this.attributes[name] = value }, addEventListener() {},
                    querySelector: selector => selector === 'pre' ? pre : firstButton,
                    getBoundingClientRect() {
                        const bounds = options.overlayBounds || { width: 200, height: 100 }
                        return {
                            width: Math.min(bounds.width, parseFloat(this.style.maxWidth) || Infinity),
                            height: Math.min(bounds.height, parseFloat(this.style.maxHeight) || Infinity),
                        }
                    },
                    contains(target) { return target === this || target.overlayParent === this },
                    remove() { this.removed = true; if (overlay === this) overlay = null },
                    showModal() { this.open = true },
                }
            },
            documentElement: { scrollTop: 0 },
            getElementById: id => id === 'image-debug-dialog' ? dialog : controls[id] || node,
        },
        location, history, URL,
        scrollTo(x, y) { this.document.documentElement.scrollTop = y },
        IMG_HOSTS: { '2024': 'https://archive.example' },
        CB: 'test',
        Image: class {
            width = 1
            height = 1
            constructor() {
                if (options.decode) this.decode = () => options.decode(this, this.src)
            }
            get src() { return this.url }
            set src(value) {
                this.url = value
                if (value.startsWith('data:image/webp')) {
                    if (options.webpSupported === false) this.onerror()
                    else this.onload()
                    return
                }
                imageRequests.push(this)
                if (options.imageLoad) options.imageLoad(this, value)
                // Existing tests keep originals pending so preview assertions are stable.
                else if (value.startsWith('images/')) this.onload()
            }
        },
        console: { warn() {} },
        setTimeout: options.setTimeout || setTimeout,
        clearTimeout: options.clearTimeout || clearTimeout,
        requestAnimationFrame: options.requestAnimationFrame || (callback => callback()),
        fetchJson: async path => {
            requests.push(path)
            if (path === 'images/dir_index.json') return options.dirIndex || (extraArchive
                ? { '2024/01': 2, '2024/02': 2, '2024/03': 2 } : { '2024/01': entries.length })
            if (/^images\/\d{4}\/\d{2}\/telegram_metadata\.json$/.test(path)) return options.telegram || { '2024-01-02_b.jpg': [42, 7, 100, 2] }
            if (path === 'images/classification_catalog.json') {
                const index = await classifications
                const tagCounts = {}, templateCounts = {}
                for (const entry of Object.values(index)) {
                    const tags = [...(entry.tags || []), ...(entry.tags_de || []), ...(entry.tags_en || [])]
                    for (const tag of new Set(tags.map(tag => tag.normalize('NFC').trim().toLowerCase()))) {
                        tagCounts[tag] = (tagCounts[tag] || 0) + 1
                    }
                    if (entry.template) templateCounts[entry.template] = (templateCounts[entry.template] || 0) + 1
                }
                return { tagCounts: Object.fromEntries(Object.entries(tagCounts).filter(([, count]) => count > 1)), templateCounts }
            }
            if (/^images\/\d{4}\/\d{2}\/classification_index\.json$/.test(path)) {
                const index = await classifications
                const month = path.split('/').slice(1, 3).join('/') + '/'
                return Object.fromEntries(Object.entries(index).filter(([key]) => key.startsWith(month))
                    .map(([key, { text, description, ...entry }]) => [key, entry]))
            }
            if (/^images\/\d{4}\/\d{2}\/classification_text_index\.json$/.test(path)) {
                if (options.textIndex) return options.textIndex
                const index = await classifications
                const month = path.split('/').slice(1, 3).join('/') + '/'
                return Object.fromEntries(Object.entries(index).filter(([key]) => key.startsWith(month))
                    .map(([key, { text, description }]) => [key, { text, description }]))
            }
            if (extraArchive && !path.includes('/2024/01/')) return entries.map(item => ({
                ...item, name: item.name.replace('2024-01', path.includes('/2024/02/') ? '2024-02' : '2024-03'),
            }))
            return entries
        },
        innerWidth: options.innerWidth || 400,
        innerHeight: options.innerHeight || 4 * 305,
        addEventListener: (event, handler) => {
            (listeners[event] ||= []).push(handler)
        },
        lightbox,
    }
    node.getBoundingClientRect = () => ({ top: (options.galleryTop || 0) - context.document.documentElement.scrollTop })
    context.window = context
    context.Worker = workerClass(path => context.fetchJson(path), options.worker || {})
    vm.runInNewContext(appSource, context)
    return { node, controls, resolveClassifications, rejectClassifications, listeners, lightbox, requests, imageRequests, context,
        get dialog() { return dialog }, get overlay() { return overlay } }
}

// Model fitted tag markup, including its CSS visibility and accessibility state.
// Unlike static tagGroups, these elements are recreated on every innerHTML write.
function classificationElement(markup, imageId, width, measurements = new Map()) {
    const buttonPattern = /<button\b([^>]*)>([\s\S]*?)<\/button>/g
    const children = [...markup.matchAll(buttonPattern)].map(([, attributes, textContent]) => {
        const attrs = Object.fromEntries([...attributes.matchAll(/([-\w]+)="([^"]*)"/g)]
            .map(([, name, value]) => [name, value]))
        const button = {
            attrs, textContent,
            hidden: /\bhidden(?:\s|=|$)/.test(attributes),
            disabled: /\bdisabled(?:\s|=|$)/.test(attributes),
            tabIndex: attrs.tabindex === undefined ? undefined : Number(attrs.tabindex),
            style: Object.fromEntries((attrs.style || '').split(';').filter(part => part.trim())
                .map(part => part.trim().split(':').map(value => value.trim()))),
            get width() { return 8 + this.textContent.length * 6 },
            getAttribute(name) { return this.attrs[name] },
            setAttribute(name, value) { this.attrs[name] = value },
            classList: { contains: name => (attrs.class || '').split(' ').includes(name) },
            getBoundingClientRect() {
                let x = 0, y = 0
                for (const child of children) {
                    if (child.style.display === 'none' || child.hidden) continue
                    if (x && x + child.width > width) { x = 0; y += 21 }
                    if (child === this) return { left: x, right: x + child.width, top: y, bottom: y + 18 }
                    x += child.width + 3
                }
                return { left: 0, right: 0, top: 0, bottom: 0 }
            },
            get outerHTML() {
                const attrs = { ...this.attrs }
                if (Object.keys(this.style).length) {
                    attrs.style = Object.entries(this.style).map(([name, value]) => `${name}: ${value};`).join(' ')
                }
                if (this.tabIndex !== undefined) attrs.tabindex = this.tabIndex
                for (const name of ['hidden', 'disabled']) {
                    if (this[name]) attrs[name] = ''
                    else delete attrs[name]
                }
                return '<button ' + Object.entries(attrs).map(([name, value]) => `${name}="${value}"`).join(' ') +
                    '>' + this.textContent + '</button>'
            },
        }
        return button
    })
    return {
        getAttribute: name => name === 'data-image-id' ? imageId : null,
        group: markup.includes('class="classification-tags"') ? {
            children,
            getBoundingClientRect() {
                measurements.set(imageId, (measurements.get(imageId) || 0) + 1)
                return { top: 0, bottom: 39, left: 0, right: width }
            },
        } : null,
        get outerHTML() {
            let i = 0
            return markup.replace(buttonPattern, () => children[i++].outerHTML)
        },
    }
}

test('Telegram request waits for the initial gallery paint, then enriches visible cards', async () => {
    const frames = []
    let release, sprite
    const telegram = new Promise(resolve => { release = resolve })
    const ui = gallery(false, [], null, 'localhost', {
        telegram, requestAnimationFrame: callback => frames.push(callback),
        imageLoad: image => { sprite = image },
    })
    await flush()
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.match(ui.node.innerHTML, /♥ —/)
    assert.ok(!ui.requests.includes('images/2024/01/telegram_metadata.json'))
    assert.equal(frames.length, 0)
    sprite.onload()
    await flush()
    frames.shift()()
    await flush()
    assert.ok(!ui.requests.includes('images/2024/01/telegram_metadata.json'))
    frames.shift()()
    await flush()
    assert.ok(ui.requests.includes('images/2024/01/telegram_metadata.json'))
    assert.match(ui.node.innerHTML, /♥ —/)
    release({ '2024-01-02_b.jpg': [42, 7, 100, 2] })
    await flush()
    assert.match(ui.node.innerHTML, /♥ 7/)
    assert.match(ui.node.innerHTML, /RosaroterPanzerBackup\/42/)
    assert.match(ui.node.innerHTML, /◉ 100/)
    assert.match(ui.node.innerHTML, /💬 2/)
    ui.resolveClassifications({})
    await flush()
})

test('text request starts last and delayed text enables full-text search and tooltips', async () => {
    let releaseTelegram, releaseText
    const telegram = new Promise(resolve => { releaseTelegram = resolve })
    const textIndex = new Promise(resolve => { releaseText = resolve })
    const ui = gallery(false, [], null, 'localhost', { telegram, textIndex })
    await flush()
    ui.resolveClassifications({ [imageId]: result, '2023/12/other.jpg': result })
    await flush()
    assert.match(ui.node.innerHTML, /class="classification-tag">Sesamstraße/)
    assert.ok(!ui.requests.includes('images/2024/01/classification_text_index.json'))
    releaseTelegram({ '2024-01-02_b.jpg': [42, 7, 100, 2] })
    await flush()
    assert.equal(ui.requests.at(-1), 'images/2024/01/classification_text_index.json')
    await applyFilters(ui, { 'filter-search': 'Sesamstraße' })
    assert.equal(ui.node.innerHTML, '', 'search waits for all matching fields so later OCR matches cannot shift results')
    assert.match(ui.controls['filter-status'].textContent, /Volltextsuche wird geladen/)
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.equal(ui.node.innerHTML, '')
    releaseText({ [imageId]: { text: result.text, description: result.description } })
    await flush()
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.match(ui.node.innerHTML, /aria-label="Description with/)
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.match(ui.controls['filter-search'].placeholder, /Bildtexte/)
})

test('text failure preserves tags, templates and image browsing', async () => {
    let rejectText
    const textIndex = new Promise((resolve, reject) => { rejectText = reject })
    const ui = gallery(false, [], null, 'localhost', { textIndex })
    await flush()
    ui.resolveClassifications({ [imageId]: result, '2023/12/other.jpg': result })
    await flush()
    rejectText(new Error('offline'))
    await flush()
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.match(ui.node.innerHTML, /meme-template/)
    await applyFilters(ui, { 'filter-search': 'Sesamstraße' })
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.match(ui.controls['filter-status'].textContent, /Volltextsuche nicht verfügbar/)
    assert.match(ui.controls['filter-search'].placeholder, /Volltextsuche nicht verfügbar/)
})

test('reaction filtering refreshes when delayed Telegram metadata arrives', async () => {
    let release
    const telegram = new Promise(resolve => { release = resolve })
    const ui = gallery(false, [], null, 'localhost', { telegram })
    await flush()
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    await applyFilters(ui, { 'filter-reactions': '7', 'filter-search': 'OCR' })
    assert.equal(ui.node.innerHTML, '')
    assert.match(ui.controls['filter-status'].textContent, /Reaktionen werden geladen/)
    release({ '2024-01-02_b.jpg': [42, 7, 100, 2] })
    await flush()
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.match(ui.node.innerHTML, /♥ 7/)
})

test('Telegram metadata failure leaves the gallery usable', async () => {
    let reject
    const telegram = new Promise((resolve, fail) => { reject = fail })
    const ui = gallery(false, [], null, 'localhost', { telegram })
    await flush()
    ui.resolveClassifications({ [imageId]: result })
    reject(new Error('offline'))
    await flush()
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.match(ui.node.innerHTML, /♥ —/)
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    await applyFilters(ui, { 'filter-reactions': '1' })
    assert.match(ui.controls['filter-status'].textContent, /Reaktionen nicht verfügbar/)
})

test('debug is opt-in with debug=1 on any host', async () => {
    for (const hostname of ['localhost', '127.0.0.1', '[::1]', 'example.com']) {
        for (const query of ['', '?debug=0', '?debug=true', '?debug=1', '?ref=cat&debug=1']) {
            const ui = gallery(false, [], null, hostname, { query })
            await flush()
            const enabled = new URL(ui.context.location.href).searchParams.get('debug') === '1'
            assert.equal(ui.node.innerHTML.includes('class="thumbnail-debug"'), enabled)
            if (!enabled) {
                ui.listeners.click[0]({
                    target: { classList: { contains: name => name === 'thumbnail-debug' } },
                    preventDefault() {},
                })
                assert.equal(ui.dialog, undefined)
            }
            ui.resolveClassifications({})
            await flush()
        }
    }
})

test('debug modal preserves all metadata safely and follows filtered indexes', async () => {
    const ui = gallery(false, [], null, 'localhost', { query: '?debug=1' })
    await flush()
    const click = index => ui.listeners.click[0]({
        target: { classList: { contains: name => name === 'thumbnail-debug' }, getAttribute: () => String(index) },
        preventDefault() {},
    })
    click(0)
    assert.equal(ui.dialog.open, true)
    let metadata = JSON.parse(ui.dialog.querySelector('pre').textContent)
    assert.equal(metadata.classificationStatus, 'loading')
    assert.equal(metadata.classification, null)
    assert.deepEqual(metadata.entry, { name: '2024-01-02_b.jpg', w: 500, h: 400, x: 0, y: 0 })
    assert.deepEqual(metadata.telegram, [42, 7, 100, 2])
    ui.resolveClassifications({ [imageId]: { ...result, extraField: { confidence: 0.9 } } })
    await flush()
    await applyFilters(ui, { 'filter-search': 'OCR' })
    click(0)
    metadata = JSON.parse(ui.dialog.querySelector('pre').textContent)
    assert.equal(metadata.imageId, imageId)
    const { text, description, ...compact } = result
    assert.deepEqual(metadata.classification, { ...compact, extraField: { confidence: 0.9 } })
    assert.deepEqual(metadata.classificationText, { text, description })
    assert.equal(metadata.gallery.src, 'https://archive.example/images/2024/01/2024-01-02_b.jpg')
    assert.equal(ui.lightbox.openedIndex, undefined)
    await applyFilters(ui, { 'filter-search': '' })
    click(1)
    metadata = JSON.parse(ui.dialog.querySelector('pre').textContent)
    assert.equal(metadata.telegram, null)
    assert.equal(metadata.classification, null)
})

const imageId = '2024/01/2024-01-02_b.jpg'
const result = {
    tags: ['Bert', '<img src=x onerror="alert(1)">', 'Sesamstraße'],
    template: 'Example "template"', template_status: 'recognized',
    description: 'Description with <markup> & "quotes"', text: 'OCR',
}

test('renders before classifications arrive, then enriches metadata safely', async () => {
    const ui = gallery()
    await flush()
    assert.match(ui.node.innerHTML, /Klassifizierung wird geladen…/)
    assert.match(ui.node.innerHTML, /https:\/\/archive.example\/images\/2024\/01\/2024-01-02_b.jpg/)
    assert.match(ui.node.innerHTML, /RosaroterPanzerBackup\/42/)
    assert.equal(ui.node.style.height, '305px')

    ui.resolveClassifications({ [imageId]: result, '2023/12/not-visible.jpg': result })
    await flush()
    assert.match(ui.node.innerHTML, /aria-label="Vorlage und Schlagwörter zum Bild"><button type="button" data-template="Example &quot;template&quot;" title="Example &quot;template&quot;" aria-label="Example &quot;template&quot;" class="classification-tag meme-template">Example &quot;template&quot;<\/button>/)
    assert.match(ui.node.innerHTML, /class="classification-tag">Bert/)
    assert.match(ui.node.innerHTML, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/)
    assert.doesNotMatch(ui.node.innerHTML, /<img/)
    assert.match(ui.node.innerHTML, /aria-label="Description with &lt;markup&gt; &amp; &quot;quotes&quot;"/)
    assert.match(ui.node.innerHTML, /Nicht klassifiziert/)
    assert.doesNotMatch(ui.node.innerHTML, /Klassifizierung wird geladen/)

    ui.listeners.click[0]({
        target: { classList: { contains: name => name === 'thumbnail' }, getAttribute: () => '0' },
        preventDefault() {},
    })
    assert.equal(ui.lightbox.openedIndex, 0)
    assert.equal(ui.lightbox.options.dataSource[0].src, 'https://archive.example/images/2024/01/2024-01-02_b.jpg')
})

test('only whole tags that fit remain visible and keyboard-accessible', async () => {
    const tag = rect => ({ style: {}, getBoundingClientRect: () => rect })
    const fitting = tag({ bottom: 18, left: 0, right: 80 })
    const secondRow = tag({ top: 21, bottom: 39, left: 0, right: 80 })
    const thirdRow = tag({ top: 42, bottom: 60, left: 0, right: 80 })
    const clippedRow = tag({ top: 63, bottom: 81, left: 0, right: 80 })
    const tooWide = tag({ bottom: 18, left: 0, right: 160 })
    const ui = gallery(false, [{
        getBoundingClientRect: () => ({ bottom: 39, left: 0, right: 150 }),
        children: [fitting, secondRow, thirdRow, clippedRow, tooWide],
    }])
    await flush()
    assert.equal(fitting.style.visibility, 'visible')
    assert.equal(fitting.tabIndex, 0)
    assert.equal(secondRow.style.visibility, 'visible', 'two lines must fit including the row gap')
    assert.equal(secondRow.tabIndex, 0)
    for (const tag of [thirdRow, clippedRow, tooWide]) {
        assert.equal(tag.style.visibility, 'hidden')
        assert.equal(tag.style.display, 'none', 'omitted tags must not occupy layout space')
        assert.equal(tag.disabled, true)
        assert.equal(tag.tabIndex, -1)
    }
    ui.resolveClassifications({})
    await flush()
})

function moreButton(group = null, hiddenTags = false) {
    group ||= { children: [], getBoundingClientRect: () => ({ top: 30 }) }
    const more = {
        style: {}, textContent: '+0',
        get width() { return 8 + this.textContent.length * 6 },
        closest: selector => selector === '.classification-tags' ? group : null,
        attributes: { 'data-image-id': imageId, 'data-hidden-tags': String(hiddenTags), 'aria-expanded': 'false' },
        getAttribute(name) { return this.attributes[name] },
        setAttribute(name, value) { this.attributes[name] = value },
        classList: { contains: name => name === 'classification-more' },
        parentElement: group,
        getBoundingClientRect() {
            let x = 0, y = 0
            for (const child of group.children) {
                if (child.style.display === 'none' || child.hidden) continue
                if (x && x + child.width > 150) { x = 0; y += 21 }
                if (child === this) return { left: x, right: x + child.width, top: y, bottom: y + 18 }
                x += child.width + 3
            }
            return { left: 126, right: 150, top: 50, bottom: 60 }
        },
        isConnected: true,
        focus() { this.focused = true },
    }
    group.children.push(more)
    return more
}

function clickMore(ui, trigger, extra = {}) {
    ui.listeners.click[0]({ target: trigger, detail: 1, clientX: 80, clientY: 100, preventDefault() {}, ...extra })
}

test('counter only appears for overflow and counts tags omitted to make room for itself', async () => {
    const small = flowingTags([80])
    const full = flowingTags([140, 140, 80])
    const empty = flowingTags([])
    const groups = [small.group, full.group, empty.group]
    const buttons = groups.map(group => moreButton(group))
    const ui = gallery(false, groups)
    await flush()
    assert.equal(buttons[0].hidden, true)
    assert.equal(buttons[1].hidden, false)
    assert.equal(buttons[2].hidden, true, 'an empty tag group needs no overflow button')
    assert.equal(full.tags[1].style.display, 'none', 'make room for the inline counter')
    assert.equal(full.tags[1].disabled, true)
    assert.equal(full.tags[1].tabIndex, -1)
    assert.equal(full.tags[2].style.visibility, 'visible')
    assert.equal(buttons[1].textContent, '+1')
    assert.equal(buttons[1].attributes['aria-label'], '1 weitere Schlagwörter anzeigen')
    assert.equal(buttons[1].getBoundingClientRect().top, full.tags[2].getBoundingClientRect().top)
    assert.ok(buttons[1].getBoundingClientRect().left > full.tags[2].getBoundingClientRect().right)
    ui.resolveClassifications({})
    await flush()
    assert.equal(buttons[1].textContent, '+1')
})

function flowingTags(widths) {
    const group = {
        getBoundingClientRect: () => ({ top: 0, bottom: 39, left: 0, right: 150 }),
        children: [],
    }
    const tags = widths.map(width => ({
        width, style: {},
        getBoundingClientRect() {
            let x = 0, y = 0
            for (const tag of tags) {
                if (tag.style.display === 'none') continue
                if (x && x + tag.width > 150) { x = 0; y += 21 }
                if (tag === this) return { top: y, bottom: y + 18, left: x, right: x + tag.width }
                x += tag.width + 3
            }
            return { top: 0, bottom: 0, left: 0, right: 0 }
        },
    }))
    group.children.push(...tags)
    return { group, tags }
}

test('clearly English tags are overlay-only while German and ambiguous tags stay eligible', async () => {
    const ui = gallery()
    await flush()
    const english = ['political satire', 'German text', 'cat', 'taxes are theft', 'two-panel meme']
    const inline = ['politische Satire', 'politischer Humor', 'blonde Frau', 'Student',
        'vier-panel-meme', 'Screenshot', 'Bitcoin', 'Star Wars', 'Bert', 'Katze', 'Comic', 'Text']
    ui.resolveClassifications({ [imageId]: { tags: [...english, ...inline], template: 'The Office' },
        'other/image.jpg': { tags: [], template: 'The Office' } })
    await flush()
    for (const tag of english) {
        assert.ok(ui.node.innerHTML.includes(`class="classification-tag" data-overlay-only="true">${tag}</button>`), tag)
    }
    for (const tag of inline) {
        assert.ok(ui.node.innerHTML.includes(`class="classification-tag">${tag}</button>`), tag)
    }
    assert.match(ui.node.innerHTML, /class="classification-tag meme-template">The Office/,
        'recognized templates are not language-filtered')
    clickMore(ui, moreButton())
    for (const tag of [...english, ...inline]) assert.ok(ui.overlay.innerHTML.includes(`>${tag}</button>`), tag)
    assert.doesNotMatch(ui.overlay.innerHTML, /data-overlay-only/)
    await applyFilters(ui, { 'filter-search': 'taxes are theft' })
    assert.equal(thumbnailCount(ui), 1, 'English tags remain searchable')
})

test('explicit German lists override vocabulary guesses and retain all English tags for search and overlay', async () => {
    const ui = gallery()
    await flush()
    // Names can contain English words; explicit metadata, not vocabulary, decides.
    const tags = ['Government', 'Katze', 'Bitcoin', 'rhinoceros', 'cat']
    ui.resolveClassifications({ [imageId]: {
        tags, tags_de: ['government', 'Katze', 'Bitcoin'],
        tags_en: ['Government', 'Bitcoin', 'rhinoceros', 'cat'], template: null,
    } })
    await flush()
    for (const tag of ['Government', 'Katze', 'Bitcoin']) {
        assert.ok(ui.node.innerHTML.includes(`class="classification-tag">${tag}</button>`), tag)
    }
    for (const tag of ['rhinoceros', 'cat']) {
        assert.ok(ui.node.innerHTML.includes(`data-overlay-only="true">${tag}</button>`), tag)
    }
    clickMore(ui, moreButton())
    for (const tag of tags) assert.ok(ui.overlay.innerHTML.includes(`>${tag}</button>`), tag)
    assert.doesNotMatch(ui.overlay.innerHTML, /data-overlay-only/)
    for (const search of ['rhinoceros', 'cat']) {
        await applyFilters(ui, { 'filter-search': search })
        assert.equal(thumbnailCount(ui), 1, 'English tags remain searchable')
    }
})

test('neutral format displays German plus shared and unresolved tags while searching all three lists', async () => {
    const ui = gallery()
    await flush()
    const neutral = ['Bitcoin', 'unresolved government thing']
    const german = ['Katze']
    const english = ['rhinoceros', 'cat']
    ui.resolveClassifications({ [imageId]: {
        tags: neutral, tags_de: german, tags_en: english, tag_format_version: 2, template: null,
    } })
    await flush()
    for (const tag of [...neutral, ...german]) {
        assert.ok(ui.node.innerHTML.includes(`class="classification-tag">${tag}</button>`), tag)
    }
    for (const tag of english) {
        assert.ok(ui.node.innerHTML.includes(`data-overlay-only="true">${tag}</button>`), tag)
    }
    clickMore(ui, moreButton())
    for (const tag of [...neutral, ...german, ...english]) {
        assert.ok(ui.overlay.innerHTML.includes(`>${tag}</button>`), tag)
    }
    assert.doesNotMatch(ui.overlay.innerHTML, /data-overlay-only/)
    for (const search of [...neutral, ...german, ...english]) {
        await applyFilters(ui, { 'filter-search': search })
        assert.equal(thumbnailCount(ui), 1, 'search includes every tag group')
    }
})

test('English-only neutral-format records still have a tag group and full overlay', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({ [imageId]: {
        tags: [], tags_de: [], tags_en: ['rhinoceros'], tag_format_version: 2, template: null,
    } })
    await flush()
    assert.match(ui.node.innerHTML, /class="classification-tags"/)
    assert.ok(ui.node.innerHTML.includes('data-overlay-only="true">rhinoceros</button>'))
    clickMore(ui, moreButton())
    assert.ok(ui.overlay.innerHTML.includes('>rhinoceros</button>'))
    await applyFilters(ui, { 'filter-search': 'rhinoceros' })
    assert.equal(thumbnailCount(ui), 1)
})

test('explicit empty German list does not fall back to language guessing', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({ [imageId]: {
        tags: ['rhinoceros', 'Bitcoin'], tags_de: [], tags_en: ['rhinoceros', 'Bitcoin'], template: null,
    } })
    await flush()
    for (const tag of ['rhinoceros', 'Bitcoin']) {
        assert.ok(ui.node.innerHTML.includes(`data-overlay-only="true">${tag}</button>`), tag)
    }
    clickMore(ui, moreButton())
    assert.ok(ui.overlay.innerHTML.includes('>rhinoceros</button>'))
    assert.ok(ui.overlay.innerHTML.includes('>Bitcoin</button>'))
})

test('overlay-only tags take no inline space, are not focusable, and are counted even without overflow', async () => {
    const mixed = flowingTags([140, 140, 80])
    const englishOnly = flowingTags([80, 80])
    const englishTags = [...mixed.tags.slice(0, 2), ...englishOnly.tags]
    for (const tag of englishTags) tag.getAttribute = name => name === 'data-overlay-only' ? 'true' : null
    const mixedMore = moreButton(mixed.group)
    const englishMore = moreButton(englishOnly.group)
    const ui = gallery(false, [mixed.group, englishOnly.group])
    await flush()
    const check = () => {
        for (const tag of englishTags) {
            assert.equal(tag.style.display, 'none')
            assert.equal(tag.style.visibility, 'hidden')
            assert.equal(tag.disabled, true)
            assert.equal(tag.tabIndex, -1)
        }
        assert.equal(mixed.tags[2].style.visibility, 'visible')
        assert.equal(mixed.tags[2].getBoundingClientRect().top, 0, 'English tags do not use a row')
        for (const more of [mixedMore, englishMore]) {
            assert.equal(more.hidden, false)
            assert.equal(more.textContent, '+2')
            assert.ok(more.getBoundingClientRect().bottom <= 39)
        }
    }
    check()
    ui.resolveClassifications({})
    await flush()
    check()
})

test('counter reserves space for multi-digit hidden counts and repacks on subsequent renders', async () => {
    const { group, tags } = flowingTags([...Array(14).fill(140), 20])
    const more = moreButton(group)
    const ui = gallery(false, [group])
    await flush()
    assert.equal(more.textContent, '+13')
    assert.equal(tags.filter(tag => tag.disabled).length, 13)
    assert.ok(more.getBoundingClientRect().bottom <= group.getBoundingClientRect().bottom)
    assert.ok(more.getBoundingClientRect().right <= group.getBoundingClientRect().right)
    for (const tag of tags) tag.width = 10
    ui.resolveClassifications({})
    await flush()
    assert.equal(more.hidden, true, 'hide the counter when all tags fit after repacking')
    assert.ok(tags.every(tag => !tag.disabled && tag.tabIndex === 0))
})

test('a rejected long tag frees the second row for a later shorter tag', async () => {
    const { group, tags } = flowingTags([140, 80, 90, 35])
    const more = moreButton(group)
    const ui = gallery(false, [group], null, 'localhost', { moreButtons: [more] })
    await flush()
    assert.equal(tags[2].style.display, 'none')
    assert.equal(tags[2].disabled, true)
    assert.equal(tags[3].style.visibility, 'visible')
    assert.equal(tags[3].style.display, '')
    assert.equal(tags[3].disabled, false)
    assert.equal(tags[3].tabIndex, 0)
    assert.equal(tags[3].getBoundingClientRect().top, 21)
    assert.equal(more.hidden, false)
    ui.resolveClassifications({})
    await flush()
    assert.equal(tags[3].style.visibility, 'visible', 'repeated fitting must reset and repack all candidates')
})

test('a tag rejected to reserve counter space leaves room for subsequent shorter tags', async () => {
    const { group, tags } = flowingTags([140, 140, 80, 20])
    const more = moreButton(group)
    const ui = gallery(false, [group], null, 'localhost', { moreButtons: [more] })
    await flush()
    assert.equal(tags[1].style.display, 'none')
    assert.equal(tags[2].style.visibility, 'visible')
    assert.equal(tags[3].style.visibility, 'visible')
    assert.equal(tags[3].getBoundingClientRect().top, 21)
    assert.equal(more.hidden, false)
    ui.resolveClassifications({})
    await flush()
})

test('counter opens all tags aligned with the tag group, safely, without changing gallery geometry', async () => {
    const ui = gallery()
    await flush()
    const tags = ['Bert', 'cat', 'katze', '<img src=x onerror="alert(1)">']
    ui.resolveClassifications({ [imageId]: { tags, template: 'Drake Hotline Bling' } })
    await flush()
    assert.match(ui.node.innerHTML, /class="classification-more"[^>]*>\+0<\/button><\/div>/)
    assert.match(ui.node.innerHTML, /class="classification-tags"[^>]*><button[^>]*class="classification-tag"/)
    const html = ui.node.innerHTML, height = ui.node.style.height
    const trigger = moreButton(null, true)
    clickMore(ui, trigger, { target: {
        classList: { contains: () => false },
        closest: selector => selector === '.classification-more' ? trigger : null,
    } })
    assert.equal(trigger.attributes['aria-expanded'], 'true', 'clicks on the counter open the overlay')
    assert.equal(ui.overlay.attributes.role, 'dialog')
    assert.equal(ui.overlay.style.left, '92px')
    assert.equal(ui.overlay.style.top, '30px')
    assert.doesNotMatch(ui.overlay.innerHTML, /Alle Schlagwörter|<h2/)
    assert.equal(ui.overlay.attributes['aria-label'], 'Schlagwörter zum Bild')
    assert.match(ui.overlay.innerHTML, /class="classification-tag meme-template" data-template="Drake Hotline Bling">Drake Hotline Bling/)
    for (const tag of ['Bert', 'cat', 'katze']) assert.ok(ui.overlay.innerHTML.includes(`>${tag}</button>`))
    assert.match(ui.overlay.innerHTML, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/)
    assert.doesNotMatch(ui.overlay.innerHTML, /<img/)
    assert.equal(ui.overlay.querySelector('.classification-tag').focused, true)
    assert.equal(ui.overlay.querySelector('.classification-tag').focusOptions.preventScroll, true)
    assert.equal(ui.node.innerHTML, html)
    assert.equal(ui.node.style.height, height)
    assert.equal(ui.lightbox.openedIndex, undefined)
    clickMore(ui, trigger)
    assert.equal(ui.overlay, null, 'clicking the counter again closes it')
    assert.equal(trigger.attributes['aria-expanded'], 'false')
})

test('tag overlay clamps to viewport edges and supports keyboard opening and Escape', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({ [imageId]: { tags: ['cat', 'katze'], template: null } })
    await flush()
    const trigger = moreButton(null, true)
    clickMore(ui, trigger, { clientX: 398, clientY: ui.context.innerHeight - 1 })
    assert.equal(ui.overlay.style.left, '192px')
    assert.equal(ui.overlay.style.top, '30px', 'pointer height does not change the group alignment')
    ui.listeners.keydown[0]({ key: 'Escape', preventDefault() {} })
    assert.equal(ui.overlay, null)
    assert.equal(trigger.focused, true)
    clickMore(ui, trigger, { detail: 0, clientX: 0, clientY: 0 })
    assert.equal(ui.overlay.style.left, `${trigger.getBoundingClientRect().right + 12}px`,
        'keyboard opening anchors at the button')
    assert.equal(ui.overlay.style.top, '30px')
})

test('overlay moves up near the viewport bottom without reducing its height limit', async () => {
    for (const contentHeight of [100, 400]) {
        const ui = gallery(false, [], null, 'localhost', {
            overlayBounds: { width: 350, height: contentHeight },
        })
        await flush()
        ui.resolveClassifications({ [imageId]: { tags: ['cat'], template: null } })
        await flush()
        const top = ui.context.innerHeight - 60
        const trigger = moreButton({ children: [], getBoundingClientRect: () => ({ top }) })
        clickMore(ui, trigger, { clientX: ui.context.innerWidth - 1 })
        const expectedHeight = Math.min(contentHeight, 250)
        assert.equal(ui.overlay.style.top, `${ui.context.innerHeight - expectedHeight - 8}px`)
        assert.equal(ui.overlay.style.maxHeight, '250px')
        assert.equal(ui.overlay.style.left, '42px')
        assert.equal(ui.overlay.getBoundingClientRect().height, expectedHeight)
        clickMore(ui, trigger)
        clickMore(ui, trigger, { clientX: -20 })
        assert.equal(ui.overlay.style.left, '8px', 'keep the overlay inside the left edge')
    }
})

test('overlay measures with full viewport limits before positioning in a small viewport', async () => {
    const ui = gallery(false, [], null, 'localhost', {
        innerWidth: 300, innerHeight: 200, overlayBounds: { width: 350, height: 400 },
    })
    await flush()
    ui.resolveClassifications({ [imageId]: { tags: ['cat'], template: null } })
    await flush()
    // Client dimensions exclude the browser scrollbars.
    ui.context.document.documentElement.clientWidth = 285
    ui.context.document.documentElement.clientHeight = 185
    const trigger = moreButton({ children: [], getBoundingClientRect: () => ({ top: 180 }) })
    clickMore(ui, trigger, { clientX: 290 })
    assert.equal(ui.overlay.style.maxWidth, '269px')
    assert.equal(ui.overlay.style.maxHeight, '169px')
    assert.equal(ui.overlay.style.left, '8px')
    assert.equal(ui.overlay.style.top, '8px')
})

test('overlay tag clicks still filter, and outside click, close button, scroll and resize dismiss it', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({ [imageId]: { tags: ['cat', 'katze'], template: null } })
    await flush()
    const trigger = moreButton(null, true)
    clickMore(ui, trigger)
    ui.listeners.click[0]({
        target: { overlayParent: ui.overlay, textContent: 'katze',
            classList: { contains: name => name === 'classification-tag' } },
        preventDefault() {},
    })
    assert.equal(ui.controls['filter-search'].value, 'katze')
    assert.equal(ui.overlay, null)
    assert.equal(ui.lightbox.openedIndex, undefined)
    for (const event of ['outside', 'close', 'scroll', 'resize']) {
        clickMore(ui, trigger)
        assert.ok(ui.overlay)
        if (event === 'scroll' || event === 'resize') ui.listeners[event][0]({})
        else ui.listeners.click[0]({
            target: { classList: { contains: name => event === 'close' && name === 'tags-overlay-close' } },
            preventDefault() {},
        })
        assert.equal(ui.overlay, null, event)
    }
    await updateViewport(ui)
})

test('tag counter is inline while the overlay stays outside normal document flow', () => {
    const style = readFileSync(`${__dirname}/../assets/style.css`, 'utf8')
    assert.match(style, /\.classification-tags \{[^}]*position: relative;[^}]*gap: 3px;[^}]*height: 39px;/)
    assert.match(style, /\.classification-more \{[^}]*flex: 0 0 auto;[^}]*font: inherit;[^}]*white-space: nowrap;/)
    assert.doesNotMatch(style, /\.classification-more \{[^}]*position: absolute;/)
    assert.match(style, /\.classification-more\[hidden\] \{[^}]*display: none;/)
    assert.match(style, /#all-tags-overlay \{[^}]*position: fixed;[^}]*box-sizing: border-box;/)
    assert.match(style, /#all-tags-overlay \{[^}]*width: min\(350px, calc\(100vw - 16px\)\);/)
    assert.match(style, /#all-tags-overlay \{[^}]*max-height: min\(250px, calc\(100vh - 16px\)\);[^}]*overflow-y: auto;/)
    assert.match(style, /\.tags-overlay-list \.classification-tag \{[^}]*visibility: visible;/)
})

test('search input waits for 500 ms of inactivity before applying the latest value', async () => {
    let now = 0, nextId = 0
    const timers = new Map()
    const advance = ms => {
        const end = now + ms
        while (true) {
            const next = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0]
            if (!next || next[1].at > end) break
            const [id, timer] = next
            now = timer.at
            timers.delete(id)
            timer.callback()
        }
        now = end
    }
    const ui = gallery(false, [], null, 'localhost', {
        setTimeout: (callback, delay) => {
            const id = ++nextId
            timers.set(id, { callback, at: now + delay })
            return id
        },
        clearTimeout: id => timers.delete(id),
    })
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    const initialHTML = ui.node.innerHTML
    const search = ui.controls['filter-search']
    search.value = 'no matches'
    search.handler()
    advance(100)
    search.value = 'OCR'
    search.handler()
    advance(499)
    await flush()
    assert.equal(ui.node.innerHTML, initialHTML, 'typing must not clear or filter the gallery')
    assert.equal(ui.controls['filter-status'].textContent, '')
    assert.equal(timers.size, 1, 'each keystroke replaces the pending search callback')
    advance(1)
    assert.equal(ui.node.innerHTML, '', 'the filter handler runs after exactly 500 ms idle')
    advance(150)
    await flush()
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.doesNotMatch(ui.node.innerHTML, /2024-01-01_a.jpg/)
    assert.equal(timers.size, 0)

    // An immediate change to another filter applies the search and cancels its timer.
    search.value = 'no matches'
    search.handler()
    advance(100)
    ui.controls['filter-reactions'].value = '7'
    ui.controls['filter-reactions'].handler()
    advance(150)
    await flush()
    assert.equal(ui.controls['filter-status'].textContent, '0 passende Bilder')
    assert.equal(timers.size, 0, 'no redundant search handler remains queued')
})

test('search icons reflect the input value immediately, regardless of focus', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    const search = ui.controls['filter-search']
    const clear = ui.controls['filter-search-clear']
    const icon = ui.controls['filter-search-icon']
    assert.equal(clear.hidden, true)
    assert.equal(icon.hidden, false)
    for (const value of ['OCR', ' ', '']) {
        search.value = value
        search.handler()
        assert.equal(clear.hidden, value === '')
        assert.equal(icon.hidden, value !== '')
        assert.notEqual(search.focused, true)
    }
    await new Promise(resolve => setTimeout(resolve, 680))
})

test('clear search applies immediately, cancels pending input, and updates icons on history navigation', async () => {
    const ui = gallery(false, [], null, 'localhost', { query: '?q=Sesamstra%C3%9Fe' })
    await flush()
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    const search = ui.controls['filter-search']
    const clear = ui.controls['filter-search-clear']
    const icon = ui.controls['filter-search-icon']
    assert.equal(search.value, 'Sesamstraße')
    assert.equal(clear.hidden, false)
    assert.equal(icon.hidden, true)

    search.value = 'pending change'
    search.handler()
    clear.handler()
    assert.equal(search.value, '')
    assert.equal(clear.hidden, true)
    assert.equal(icon.hidden, false)
    assert.notEqual(search.focused, true, 'clear must not open the mobile keyboard')
    assert.equal(new URL(ui.context.location.href).searchParams.has('q'), false)
    await new Promise(resolve => setTimeout(resolve, 680))
    await flush()
    assert.equal(ui.controls['filter-status'].textContent, '')
    assert.equal(thumbnailCount(ui), 2)

    await ui.context.history.go(-1)
    assert.equal(search.value, 'Sesamstraße')
    assert.equal(clear.hidden, false)
    assert.equal(icon.hidden, true)
    await ui.context.history.go(1)
    assert.equal(search.value, '')
    assert.equal(clear.hidden, true)
    assert.equal(icon.hidden, false)
})

test('clicking a tag fills the search and filters without focus or opening the lightbox', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    ui.listeners.click[0]({
        target: { textContent: 'Sesamstraße', classList: { contains: name => name === 'classification-tag' } },
        preventDefault() {},
    })
    assert.equal(ui.controls['filter-search'].value, 'Sesamstraße')
    assert.notEqual(ui.controls['filter-search'].focused, true)
    assert.equal(ui.controls['filter-search-clear'].hidden, false)
    assert.equal(ui.controls['filter-search-icon'].hidden, true)
    await new Promise(resolve => setTimeout(resolve, 180))
    await flush()
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.equal(ui.lightbox.openedIndex, undefined)
})

test('clicking a template selects it, even when absent from the dropdown, without changing search', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({ [imageId]: result, '2023/12/other.jpg': result })
    await flush()
    assert.doesNotMatch(ui.controls['filter-template'].innerHTML, /Example/)
    assert.match(ui.node.innerHTML, /class="classification-tag meme-template"/)
    ui.controls['filter-search'].value = 'OCR'
    const click = () => ui.listeners.click[0]({
        target: {
            textContent: result.template,
            getAttribute: name => name === 'data-template' ? result.template : null,
            classList: { contains: name => ['classification-tag', 'meme-template'].includes(name) },
        },
        preventDefault() {},
    })
    click()
    assert.equal(ui.controls['filter-template'].value, result.template)
    assert.equal(ui.controls['filter-template'].focused, true)
    assert.match(ui.controls['filter-template'].innerHTML, /Example &quot;template&quot; \(2\)/)
    assert.equal(ui.controls['filter-search'].value, 'OCR')
    await new Promise(resolve => setTimeout(resolve, 180))
    await flush()
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.equal(ui.lightbox.openedIndex, undefined)
})

test('long template names are shortened in cards and options, but filtering uses full names', async () => {
    for (const [name, label] of [
        ['modern problems require modern solutions', 'modern problems require…'],
        ['spider-man pointing at spider-man', 'spider-man pointing at…'],
        ['mr. incredible becoming uncany', 'mr. incredible becoming…'],
    ]) {
        // Cover both the initial dropdown and options inserted by clicking a card.
        for (const count of [2, 5]) {
            const ui = gallery()
            await flush()
            const classification = { ...result, template: name, tags: [] }
            ui.resolveClassifications({
                [imageId]: classification,
                ...Object.fromEntries(Array.from({ length: count - 1 }, (_, i) => [`other/${i}`, classification])),
            })
            await flush()
            const button = ui.node.innerHTML.match(/data-template="([^"]+)" title="([^"]+)" aria-label="([^"]+)" class="classification-tag meme-template">([^<]+)<\/button>/)
            assert.ok(button)
            assert.deepEqual(button.slice(1), [name, name, name, label])
            const option = `title="${name}" value="${name}">${label} (${count})</option>`
            assert.equal(ui.controls['filter-template'].innerHTML.includes(option), count === 5)
            const click = () => ui.listeners.click[0]({
                target: {
                    textContent: button[4],
                    getAttribute: attr => attr === 'data-template' ? button[1] : null,
                    classList: { contains: value => ['classification-tag', 'meme-template'].includes(value) },
                },
                preventDefault() {},
            })
            click()
            click()
            assert.equal(ui.controls['filter-template'].value, name)
            assert.ok(ui.controls['filter-template'].innerHTML.includes(option))
            assert.equal((ui.controls['filter-template'].innerHTML.match(/<option /g) || []).length, 2)
            await new Promise(resolve => setTimeout(resolve, 180))
            await flush()
            assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
            assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
            assert.equal(ui.lightbox.openedIndex, undefined)
        }
    }
})

test('classification failure does not remove images or Telegram metadata', async () => {
    const ui = gallery()
    await flush()
    ui.rejectClassifications(new Error('HTTP 404'))
    await flush()
    assert.match(ui.node.innerHTML, /Klassifizierung nicht verfügbar/)
    assert.match(ui.node.innerHTML, /class="thumbnail"/)
    assert.match(ui.node.innerHTML, /RosaroterPanzerBackup\/42/)
})

test('unknown and absent templates do not display a template label', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({
        [imageId]: { ...result, template: null, template_status: 'unknown', tags: [] },
        '2024/01/2024-01-01_a.jpg': { ...result, template: null, template_status: 'none', tags: ['tag'] },
    })
    await flush()
    assert.doesNotMatch(ui.node.innerHTML, /Vorlage:|class="meme-template"/)
    assert.match(ui.node.innerHTML, /class="classification-tag">tag<\/button>/)
    assert.doesNotMatch(ui.node.innerHTML, /Nicht klassifiziert/)
})

test('tags use catalog-wide image counts for sorting without hiding singletons', async () => {
    const ui = gallery()
    await flush()
    const visible = {
        ...result,
        description: 'Unique visible description',
        tags: ['Singleton', 'Rare', 'Common', 'Alphabetical', 'Duplicate only', 'duplicate ONLY'],
    }
    ui.resolveClassifications({
        [imageId]: visible,
        '2023/12/other.jpg': { ...result, tags: ['rare', ' COMMON ', 'Alphabetical'] },
        '2023/11/another.jpg': { ...result, tags: ['common'] },
    })
    await flush()
    const tagLabels = () => [...ui.node.innerHTML.matchAll(/class="classification-tag">([^<]+)<\/button>/g)]
        .map(match => match[1])
    const expected = ['Common', 'Alphabetical', 'Rare', 'duplicate ONLY', 'Duplicate only', 'Singleton']
    assert.deepEqual(tagLabels(), expected)
    assert.deepEqual(visible.tags, ['Singleton', 'Rare', 'Common', 'Alphabetical', 'Duplicate only', 'duplicate ONLY'])
    // Filtering must not change frequencies or remove singleton tags from search.
    await applyFilters(ui, { 'filter-search': 'singleton' })
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.deepEqual(tagLabels(), expected)
})

test('template labels hide catalog singletons without removing images or search data', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({
        [imageId]: { ...result, template: 'Singleton template', tags: [] },
        '2024/01/2024-01-01_a.jpg': { ...result, template: 'Shared template', tags: [] },
        '2023/12/other.jpg': { ...result, template: 'Shared template', tags: [] },
    })
    await flush()
    assert.equal(thumbnailCount(ui), 2)
    assert.doesNotMatch(ui.node.innerHTML, /class="classification-tag meme-template">Singleton template/)
    assert.match(ui.node.innerHTML, /class="classification-tag meme-template">Shared template/)
    assert.doesNotMatch(ui.controls['filter-template'].innerHTML, /Singleton template|Shared template/)

    // Frequencies remain catalog-wide, even when only one matching image is shown.
    await applyFilters(ui, { 'filter-search': 'Shared template' })
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.match(ui.node.innerHTML, /class="classification-tag meme-template">Shared template/)
    await applyFilters(ui, { 'filter-search': 'Singleton template' })
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.doesNotMatch(ui.node.innerHTML, /meme-template/)
})

test('template dropdown shows usage counts and excludes templates used fewer than five times', async () => {
    const ui = gallery()
    await flush()
    const classifications = {}
    for (const [template, count] of [[result.template, 5], ['Rare', 4], ['Popular', 6]]) {
        for (let i = 0; i < count; i++) classifications[`${template}/${i}`] = { ...result, template }
    }
    classifications['no-template'] = { ...result, template: null }
    ui.resolveClassifications(classifications)
    await flush()
    const options = ui.controls['filter-template'].innerHTML
    assert.match(options, /value="Example &quot;template&quot;">Example &quot;template&quot; \(5\)/)
    assert.match(options, /value="Popular">Popular \(6\)/)
    assert.doesNotMatch(options, /Rare|null/)
    assert.equal((options.match(/<option /g) || []).length, 3)
    assert.ok(options.indexOf('Example') < options.indexOf('Popular'))
})

async function applyFilters(ui, values) {
    for (const [id, value] of Object.entries(values)) ui.controls[id].value = value
    const control = Object.keys(values).at(-1)
    ui.controls[control].handler()
    await new Promise(resolve => setTimeout(resolve, control === 'filter-search' ? 680 : 180))
    await flush()
}

test('combines minimum reactions, template and case-insensitive literal text search', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({
        [imageId]: result,
        ...Object.fromEntries(Array.from({ length: 4 }, (_, i) => [`other/${i}`, result])),
    })
    await flush()
    assert.match(ui.controls['filter-template'].innerHTML, /Example &quot;template&quot; \(5\)/)
    await applyFilters(ui, {
        'filter-reactions': '7', 'filter-template': result.template, 'filter-search': 'oCr',
    })
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.doesNotMatch(ui.node.innerHTML, /2024-01-01_a.jpg/)
    ui.listeners.click[0]({
        target: { classList: { contains: name => name === 'thumbnail' }, getAttribute: () => '0' }, preventDefault() {},
    })
    assert.equal(ui.lightbox.options.dataSource.length, 1)
    assert.equal(ui.lightbox.options.dataSource[0].imageId, imageId)
    await applyFilters(ui, { 'filter-reactions': '8' })
    assert.equal(ui.node.innerHTML, '')
    assert.equal(ui.node.style.height, '0px')
    assert.equal(ui.controls['filter-status'].textContent, '0 passende Bilder')
    await applyFilters(ui, { 'filter-reactions': '0', 'filter-template': '', 'filter-search': '' })
    assert.match(ui.node.innerHTML, /2024-01-01_a.jpg/)
    assert.equal(ui.node.style.height, '305px')
})

for (const failFirst of [false, true]) {
    test(`month scheduling refills slots independently${failFirst ? ' after failure' : ''}`, async () => {
        const dirs = Array.from({ length: 10 }, (_, i) => `2024/${String(i + 1).padStart(2, '0')}`)
        const entries = ['2024-01-01_a.jpg', '2024-01-02_b.jpg'].map(name => ({ name, w: 500, h: 400 }))
        const ui = gallery(false, [], entries, 'localhost', {
            dirIndex: Object.fromEntries(dirs.map(dir => [dir, entries.length])),
        })
        ui.resolveClassifications(Object.fromEntries(dirs.flatMap(dir =>
            entries.map(entry => [`${dir}/${entry.name}`, result]))))
        await flush()
        const fetchJson = ui.context.fetchJson
        const pending = new Map()
        const requested = []
        const cached = new Map()
        // Match production caching: the two initially displayed months are ready.
        for (const dir of dirs.slice(-2)) cached.set(`images/${dir}/entry_index.json`, Promise.resolve(entries))
        ui.context.fetchJson = path => {
            if (!path.endsWith('/entry_index.json')) return fetchJson(path)
            if (!cached.has(path)) {
                requested.push(path)
                cached.set(path, new Promise((resolve, reject) => {
                    pending.set(path, { resolve, reject })
                }))
            }
            return cached.get(path)
        }
        await applyFilters(ui, { 'filter-search': 'OCR' })
        const pathFor = month => `images/2024/${month}/entry_index.json`
        assert.deepEqual(requested, ['08', '07', '06', '05', '04', '03'].map(pathFor))
        assert.equal(pending.size, 6)
        const settle = (month, fail = false) => {
            const path = pathFor(month)
            const response = pending.get(path)
            pending.delete(path)
            if (fail) response.reject(new Error('offline'))
            else response.resolve(entries)
        }
        settle('08', failFirst)
        await flush()
        assert.equal(requested.at(-1), pathFor('02'))
        assert.ok(pending.has(pathFor('07')), 'slow neighbor must not block the next month')
        assert.equal(pending.size, 6)
        settle('02')
        await flush()
        assert.equal(requested.at(-1), pathFor('01'))
        for (const month of ['06', '05', '04', '03', '01']) settle(month)
        await flush()
        assert.match(ui.controls['filter-status'].textContent, failFirst ? /unvollständig/ : /Archiv wird geladen/)
        settle('07')
        await flush()
        assert.equal(requested.length, 8)
        assert.equal(new Set(requested).size, 8, 'each uncached month is scheduled once')
        assert.equal(ui.controls['filter-status'].textContent, failFirst
            ? '18 passende Bilder (Archiv unvollständig geladen. Ändere einen Filter, um es erneut zu versuchen.)'
            : '20 passende Bilder')
        openGalleryItem(ui, 0)
        assert.deepEqual(Array.from(ui.lightbox.options.dataSource, item => item.imageId),
            [...dirs].reverse().filter(dir => !failFirst || dir !== '2024/08')
                .flatMap(dir => [...entries].reverse().map(entry => `${dir}/${entry.name}`)))
    })
}

test('search buffers older completed months and appends matches without moving existing cards', async () => {
    const ui = gallery(true)
    const fetchJson = ui.context.fetchJson
    const held = new Map()
    const paths = ['03', '02'].map(month => `images/2024/${month}/classification_text_index.json`)
    const pending = new Map(paths.map(path => [path, new Promise(resolve => { held.set(path, resolve) })]))
    ui.context.fetchJson = path => pending.get(path) || fetchJson(path)
    ui.resolveClassifications(Object.fromEntries(['01', '02', '03'].map(month =>
        [`2024/${month}/2024-${month}-02_b.jpg`, result])))
    await flush()
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.equal(ui.node.innerHTML, '', 'older matches stay buffered while newest text is pending')
    assert.match(ui.controls['filter-status'].textContent, /0 passende Bilder.*Archiv wird geladen/)
    assert.equal(ui.node.attributes['aria-busy'], 'true')
    held.get(paths[0])(await fetchJson(paths[0]))
    await flush()
    assert.match(ui.node.innerHTML, /2024-03-02_b.jpg/)
    assert.doesNotMatch(ui.node.innerHTML, /2024-0[12]-02_b.jpg/)
    const positions = cardPositions(ui)
    assert.match(ui.controls['filter-status'].textContent, /1 passende Bilder.*Archiv wird geladen/)
    held.get(paths[1])(await fetchJson(paths[1]))
    await flush()
    assert.deepEqual(cardPositions(ui).slice(0, positions.length), positions, 'published card positions stay fixed')
    assert.equal(ui.controls['filter-status'].textContent, '3 passende Bilder')
    assert.equal(ui.node.attributes['aria-busy'], 'false')
    openGalleryItem(ui, 0)
    assert.deepEqual(Array.from(ui.lightbox.options.dataSource, item => item.imageId),
        ['03', '02', '01'].map(month => `2024/${month}/2024-${month}-02_b.jpg`))
})

test('incomplete-results notice retains a loading indicator while newer search chunks are pending', async () => {
    const ui = gallery(true)
    const fetchJson = ui.context.fetchJson
    let release
    const pending = new Promise(resolve => { release = resolve })
    const newest = 'images/2024/03/classification_text_index.json'
    ui.context.fetchJson = path => path === newest ? pending
        : path === 'images/2024/01/classification_text_index.json' ? Promise.reject(new Error('offline'))
        : fetchJson(path)
    ui.resolveClassifications(Object.fromEntries(['01', '02', '03'].map(month =>
        [`2024/${month}/2024-${month}-02_b.jpg`, result])))
    await flush()
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.equal(ui.node.innerHTML, '')
    assert.match(ui.controls['filter-status'].textContent, /unvollständig geladen.*Archiv wird geladen/)
    assert.equal(ui.node.attributes['aria-busy'], 'true')
    release(await fetchJson(newest))
    await flush()
    assert.match(ui.node.innerHTML, /2024-03-02_b.jpg/)
    assert.match(ui.node.innerHTML, /2024-02-02_b.jpg/)
    assert.match(ui.controls['filter-status'].textContent, /unvollständig geladen/)
    assert.doesNotMatch(ui.controls['filter-status'].textContent, /Archiv wird geladen/)
    assert.equal(ui.node.attributes['aria-busy'], 'false')
})

test('search renders partial matches and respects filter changes while an index is pending', async () => {
    const ui = gallery(true)
    await flush()
    const fetchJson = ui.context.fetchJson
    let release
    const pending = new Promise(resolve => { release = resolve })
    ui.context.fetchJson = path => path === 'images/2024/01/entry_index.json'
        ? pending : fetchJson(path)
    ui.resolveClassifications({
        [imageId]: result,
        '2024/03/2024-03-02_b.jpg': result,
        '2024/02/2024-02-02_b.jpg': { ...result, text: 'different' },
    })
    await flush()
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.match(ui.node.innerHTML, /2024-03-02_b.jpg/)
    assert.doesNotMatch(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder (Archiv wird geladen…)')
    await applyFilters(ui, { 'filter-search': 'different' })
    assert.match(ui.node.innerHTML, /2024-02-02_b.jpg/)
    release(await fetchJson('images/2024/01/entry_index.json'))
    await flush()
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.doesNotMatch(ui.node.innerHTML, /2024-01-02_b.jpg|2024-03-02_b.jpg/)
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.equal(ui.controls['filter-status'].textContent, '2 passende Bilder')
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
})

test('search keeps partial results after an index fails and allows retry', async () => {
    const ui = gallery(true)
    await flush()
    const fetchJson = ui.context.fetchJson
    ui.context.fetchJson = path => path === 'images/2024/01/entry_index.json'
        ? Promise.reject(new Error('offline')) : fetchJson(path)
    ui.resolveClassifications({ '2024/03/2024-03-02_b.jpg': result })
    await flush()
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.match(ui.node.innerHTML, /2024-03-02_b.jpg/)
    assert.match(ui.controls['filter-status'].textContent, /unvollständig geladen/)
    ui.context.fetchJson = fetchJson
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
})

test('search covers older unloaded directories, descriptions and tags, not regex patterns', async () => {
    const ui = gallery(true)
    await flush()
    assert.doesNotMatch(ui.node.innerHTML, /2024-01-02_b.jpg/)
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    for (const search of ['oCr', 'DESCRIPTION', 'sesamstraße', '<img src=x']) {
        await applyFilters(ui, { 'filter-search': search })
        assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
        assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    }
    await applyFilters(ui, { 'filter-search': '.*' })
    assert.equal(ui.node.innerHTML, '')
})

test('search ignores unrequested punctuation as separators or joins, but keeps requested punctuation literal', () => {
    const context = searchContext()
    const matches = (text, query) => context.compileSearch(query)(text)
    for (const punctuation of ['_', '-', '‐', '‑', '–', '—', ',', '.', ':', ';', '/', '…', '「', '」']) {
        assert.equal(matches(`Foo${punctuation}Bar`, 'foo bar'), true, punctuation)
        assert.equal(matches(`Foo${punctuation}Bar`, 'foobar'), true, punctuation)
        assert.equal(matches(`Foo${punctuation}Bar`, `foo${punctuation}bar`), true, punctuation)
        assert.equal(matches('Foo Bar', `foo${punctuation}bar`), false, punctuation)
        assert.equal(matches('FooBar', `foo${punctuation}bar`), false, punctuation)
    }
    for (const [text, query, expected] of [
        ["Don't_panic!", 'dont panic', true],
        ['Hello,\n“world”!', '  HELLO   world  ', true],
        ['foo_bar-baz', 'foo_bar baz', true],
        ['foo-bar_baz', 'foo_bar baz', false],
        ['foo—bar', 'foo-bar', false],
        ['first\nsecond', 'firstsecond', false],
        ['anything', '.*', false],
        ['literal .* pattern', '.*', true],
        ['literal [a] pattern', '[a]', true],
        ['literal a pattern', '[a]', false],
        ['C++', 'C++', true],
        ['C', 'C++', false],
    ]) {
        assert.equal(matches(text, query), expected, `${JSON.stringify(text)} / ${JSON.stringify(query)}`)
    }
})

test('search requires every term in any order and keeps quoted phrases within one field', () => {
    const context = searchContext()
    for (const [fields, query, expected] of [
        ['Steuern sind Diebstahl', 'steuern diebstahl', true],
        ['Steuern sind Diebstahl', 'diebstahl steuern', true],
        ['Steuern sind Diebstahl', '  DIEBSTAHL   steuern  ', true],
        ['Steuern sind Diebstahl', 'steuern steuern', true],
        ['Steuern sind Diebstahl', 'steuern geld', false],
        ['Steuern sind Diebstahl', 'steuer dieb', true],
        ['Steuern sind Diebstahl', 'steurn diebstahl', false],
        ['Steuern sind Diebstahl', '"steuern sind diebstahl"', true],
        ['Steuern sind Diebstahl', '"steuern diebstahl"', false],
        ['Steuern sind Diebstahl', '"diebstahl sind steuern"', false],
        ['Steuern sind Diebstahl', '"steuern sind" diebstahl', true],
        ['Steuern sind Diebstahl', 'diebstahl "steuern sind"', true],
        ['Steuern sind Diebstahl', '"steuern sind" geld', false],
        [['Steuern', 'sind Diebstahl'], 'diebstahl steuern', true],
        [['Steuern', 'sind Diebstahl'], '"steuern sind diebstahl"', false],
        [['shared tag', 'english tag'], '"shared tag" "english tag"', true],
        [['shared tag', 'english tag'], '"tag english"', false],
        ['Hello,\n“world”!', '"HELLO world"', true],
        ["Don't_panic!", '"dont panic"', true],
        ['Käse_Öl', '"KASE OL"', true],
        ['first\nsecond', '"firstsecond"', false],
        ['foo-bar hello_world', 'world foo-bar', true],
        ['foo_bar hello_world', 'world foo-bar', false],
        ['Steuern sind Diebstahl', '"steuern sind', true],
        ['Steuern sind Diebstahl', '"steuern diebstahl', false],
        ['Steuern sind Diebstahl', '"" steuern', true],
        ['Steuern sind Diebstahl', '""', false],
        ['Steuern sind Diebstahl', '"   "', false],
        [[], 'steuern', false],
    ]) {
        assert.equal(context.compileSearch(query)(fields), expected,
            `${JSON.stringify(fields)} / ${JSON.stringify(query)}`)
    }
    const matcher = context.compileSearch('steuern diebstahl')
    assert.equal(matcher(['Diebstahl', 'Steuern']), true)
    assert.equal(matcher(['Steuern']), false, 'compiled matchers can be reused across images')
    assert.equal(matcher(['Steuern sind Diebstahl']), true)
})

test('multi-term and quoted searches combine metadata filters and survive shared URLs', async () => {
    const classifications = {
        [imageId]: {
            text: 'Steuern sind Diebstahl', description: 'Eine politische Aussage', template: result.template,
            tag_format_version: 2, tags: ['shared tag'], tags_de: ['Äpfel'], tags_en: ['taxation'],
        },
        '2024/01/2024-01-01_a.jpg': {
            text: 'Steuern sind', description: 'Diebstahl', template: result.template, tags: ['taxation'],
        },
    }
    const ui = gallery()
    ui.resolveClassifications(classifications)
    await flush()
    for (const query of ['diebstahl steuern', 'taxation apfel politisch', '"Steuern sind Diebstahl" taxation']) {
        await applyFilters(ui, {
            'filter-reactions': '7', 'filter-template': result.template, 'filter-search': query,
        })
        assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder', query)
        assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
        assert.doesNotMatch(ui.node.innerHTML, /2024-01-01_a.jpg/)
        assert.equal(new URL(ui.context.location.href).searchParams.get('q'), query)
    }
    const shared = gallery(false, [], null, 'localhost', {
        query: new URL(ui.context.location.href).search,
    })
    shared.resolveClassifications(classifications)
    await flush()
    await flush()
    assert.equal(shared.controls['filter-search'].value, '"Steuern sind Diebstahl" taxation')
    assert.equal(shared.controls['filter-status'].textContent, '1 passende Bilder')
    assert.match(shared.node.innerHTML, /2024-01-02_b.jpg/)

    await applyFilters(ui, { 'filter-reactions': '0', 'filter-search': 'diebstahl steuern' })
    assert.equal(ui.controls['filter-status'].textContent, '2 passende Bilder', 'terms can span fields')
    await applyFilters(ui, { 'filter-search': '"steuern sind diebstahl"' })
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder', 'phrases cannot span fields')
    await applyFilters(ui, { 'filter-search': '"tag apfel"' })
    assert.equal(ui.controls['filter-status'].textContent, '0 passende Bilder', 'phrases cannot span tags')
})

test('search normalizes German umlauts in queries and entries without changing displayed text or URLs', async () => {
    const context = searchContext()
    for (const value of ['äöü', 'ÄÖÜ', 'aou', 'AOU', 'a\u0308o\u0308u\u0308']) {
        assert.equal(context.normalizeSearch(value), 'aou')
    }
    assert.equal(context.normalizeSearch('Straße'), 'straße', 'other letters stay unchanged')

    const ui = gallery()
    await flush()
    ui.resolveClassifications({
        [imageId]: {
            text: 'Äpfel', description: 'Öl', template: 'Übung', tags: ['Grun', 'Käse_Öl'],
        },
    })
    await flush()
    for (const search of ['apfel', 'OL', 'ubung', 'GRÜN', 'kase ol', 'KÄSE_ÖL']) {
        await applyFilters(ui, { 'filter-search': search })
        assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder', search)
        assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
        assert.equal(ui.controls['filter-search'].value, search)
        assert.equal(new URL(ui.context.location.href).searchParams.get('q'), search)
    }
    assert.match(ui.node.innerHTML, /Käse_Öl/)
})

test('punctuation-insensitive search covers text, descriptions, templates and all tag languages', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({
        [imageId]: {
            text: "Don't_panic!", description: 'Hello, world.', template: 'Example—template',
            tag_format_version: 2, tags: ['shared/tag'], tags_de: ['deutsches_stichwort'], tags_en: ['english-tag'],
        },
    })
    await flush()
    for (const search of ['dont panic', 'hello world', 'example template', 'shared tag',
        'deutsches stichwort', 'english tag', 'english-tag']) {
        await applyFilters(ui, { 'filter-search': search })
        assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder', search)
        assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    }
    await applyFilters(ui, { 'filter-search': 'english_tag' })
    assert.equal(ui.controls['filter-status'].textContent, '0 passende Bilder')
})

test('search matches phrases across line breaks in text and descriptions', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({
        [imageId]: {
            ...result,
            text: 'First\nsecond\r\nthird\rfourth',
            description: 'A description  \n\t spanning\n\nmultiple lines',
        },
    })
    await flush()
    for (const search of ['FIRST second third fourth', 'description spanning multiple lines',
        '  first   second  ', 'description\nspanning', '"FIRST second third fourth"',
        '"description spanning multiple lines"', '"first second" "multiple lines"']) {
        await applyFilters(ui, { 'filter-search': search })
        assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
        assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
        assert.doesNotMatch(ui.node.innerHTML, /2024-01-01_a.jpg/)
    }
    await applyFilters(ui, { 'filter-search': 'firstsecond' })
    assert.equal(ui.node.innerHTML, '')
})

const manyEntries = count => Array.from({ length: count }, (_, i) => ({
    name: `2024-01-${String(i).padStart(3, '0')}.jpg`, w: 440, h: 220, x: 999, y: 999,
}))

function openGalleryItem(ui, index) {
    ui.listeners.click[0]({
        target: { classList: { contains: name => name === 'thumbnail' }, getAttribute: () => String(index) },
        preventDefault() {},
    })
    return ui.lightbox.options.dataSource[index]
}

test('derives local sprite filenames and offsets from the original index, not reverse display order', async () => {
    const ui = gallery(false, [], manyEntries(41))
    await flush()
    assert.ok(ui.requests.includes('images/2024/01/entry_index.json'))
    assert.ok(ui.requests.every(path => !path.startsWith('https://')))
    assert.doesNotMatch(ui.node.innerHTML, /background-image: url\('https:/)
    const newest = openGalleryItem(ui, 0) // source index 40
    assert.equal(newest.thumbSrc, 'images/2024/01/thumbnails-02.webp?cb=test')
    assert.equal(newest.bgOffsetX, 0)
    assert.equal(newest.bgOffsetY, 0)
    const boundary = openGalleryItem(ui, 20) // source index 20
    assert.equal(boundary.thumbSrc, 'images/2024/01/thumbnails-01.webp?cb=test')
    assert.equal(boundary.bgOffsetX, 0)
    assert.equal(boundary.bgOffsetY, 0)
    const previous = openGalleryItem(ui, 21) // source index 19
    assert.equal(previous.thumbSrc, 'images/2024/01/thumbnails-00.webp?cb=test')
    assert.equal(previous.bgOffsetX, 888)
    assert.equal(previous.bgOffsetY, 666)
    assert.match(previous.src, /^https:\/\/archive.example\/images\//)
    assert.equal(ui.node.style.height, `${21 * 305}px`)
    ui.resolveClassifications({ [boundary.imageId]: { ...result, description: 'Boundary image' } })
    await flush()
    await applyFilters(ui, { 'filter-search': 'Boundary image' })
    const filtered = openGalleryItem(ui, 0)
    assert.equal(filtered.imageId, boundary.imageId)
    assert.equal(filtered.thumbSrc, boundary.thumbSrc)
    assert.equal(filtered.bgOffsetX, boundary.bgOffsetX)
})

const renderedSprites = ui => new Set([...ui.node.innerHTML.matchAll(/images\/[^']+\/thumbnails-\d+\.webp/g)].map(match => match[0]))
const thumbnailCount = ui => (ui.node.innerHTML.match(/class="thumbnail"/g) || []).length
async function updateViewport(ui, event = 'scroll') {
    ui.listeners[event][0]({ type: event, constructor: { name: 'Event' } })
    await new Promise(resolve => setTimeout(resolve, 180))
    await flush()
}

// Control only the scroll idle timer; image paint frames still run normally.
function scrollClock(ui) {
    const timers = new Map()
    let nextId = 0
    ui.context.setTimeout = callback => {
        const id = ++nextId
        timers.set(id, callback)
        return id
    }
    ui.context.clearTimeout = id => timers.delete(id)
    return {
        scroll(row) {
            ui.context.document.documentElement.scrollTop = row * 305
            ui.listeners.scroll[0]({ type: 'scroll' })
        },
        async settle() {
            const callbacks = [...timers.values()]
            timers.clear()
            for (const callback of callbacks) callback()
            await flush()
        },
        get pending() { return timers.size },
    }
}

test('overlapping cards preserve fitted tags and counters throughout scroll and idle', async () => {
    const entries = manyEntries(200)
    const measurements = new Map()
    const ui = gallery(false, [], entries, 'localhost', {
        classificationDOM: true, tagMeasurements: measurements,
    })
    const tags = ['Anarchie', 'Freiheit', 'Steuern', 'Widerstand', 'Gesellschaft', 'Staatskritik',
        'Selbstbestimmung', 'Eigentumsrechte']
    ui.resolveClassifications(Object.fromEntries(entries.map(entry => [
        `2024/01/${entry.name}`, { tags, template: null },
    ])))
    await flush()
    const imageId = `2024/01/${entries[193].name}` // Gallery index 6 stays inside every window below.
    const classification = () => ui.node.querySelectorAll('.thumbnail-classification[data-image-id]')
        .find(node => node.getAttribute('data-image-id') === imageId)
    const initial = classification().outerHTML
    const initialMeasurements = measurements.get(imageId)
    assert.match(initial, /visibility: visible;/)
    assert.match(initial, /display: none; visibility: hidden;/)
    assert.match(initial, /tabindex="-1" disabled=""/)
    assert.match(initial, /class="classification-more"[^>]*>\+[1-9]/)
    const clock = scrollClock(ui)
    const imageRequests = ui.imageRequests.length
    for (const row of [1, 2, 3]) {
        clock.scroll(row)
        await flush()
        assert.equal(classification().outerHTML, initial, 'visible tags, hidden tags and counter stay unchanged')
        assert.equal(measurements.get(imageId), initialMeasurements, 'retained cards need no new layout measurements')
        assert.equal(ui.imageRequests.length, imageRequests)
    }
    const enteringId = `2024/01/${entries[187].name}`
    assert.equal(measurements.get(enteringId), undefined, 'new cards may defer their tag fit until idle')
    await clock.settle()
    assert.equal(classification().outerHTML, initial)
    assert.equal(measurements.get(imageId), initialMeasurements, 'idle image hydration does not repack retained tags')
    assert.equal(measurements.get(enteringId), 1, 'new cards receive their first fit at idle')
})

test('fitted tag previews are invalidated by card width or classification changes', async () => {
    const entries = manyEntries(20)
    const measurements = new Map()
    const workers = []
    const ui = gallery(false, [], entries, 'localhost', {
        classificationDOM: true, tagMeasurements: measurements, worker: { instances: workers },
    })
    const tags = ['Anarchie', 'Freiheit', 'Steuern', 'Widerstand', 'Gesellschaft']
    ui.resolveClassifications(Object.fromEntries(entries.map(entry => [
        `2024/01/${entry.name}`, { tags, template: null },
    ])))
    await flush()
    const imageId = `2024/01/${entries[19].name}`
    const clock = scrollClock(ui)
    const initialMeasurements = measurements.get(imageId)
    ui.node.clientWidth = 320
    ui.listeners.resize[0]({ type: 'resize' })
    await clock.settle()
    assert.equal(measurements.get(imageId), initialMeasurements + 1, 'narrow cards must repack their tags')
    tags.push('Selbstbestimmung')
    // Worker messages are cloned; refresh the month's classification explicitly.
    workers[0].onmessage({ data: { type: 'metadata', dirName: '2024/01', kind: 'classification',
        data: { [imageId]: { tags, template: null } } } })
    clock.scroll(0)
    await clock.settle()
    assert.equal(measurements.get(imageId), initialMeasurements + 2, 'changed tag content must repack too')
    const classification = ui.node.querySelectorAll('.thumbnail-classification[data-image-id]')
        .find(node => node.getAttribute('data-image-id') === imageId)
    assert.match(classification.outerHTML, />Selbstbestimmung<\/button>/)
})

test('continuous scroll displays colors and dates without new images until idle', async () => {
    const entries = manyEntries(200).map(entry => ({ ...entry, bg: 'ABC' }))
    const ui = gallery(false, [], entries)
    ui.resolveClassifications({})
    await flush()
    const clock = scrollClock(ui)
    const initialRequests = ui.imageRequests.length
    const initialIndexRequests = ui.requests.filter(path => path.endsWith('/entry_index.json')).length
    const oldOriginals = [...originalRequests(ui)]
    for (const row of [14, 20, 30]) {
        clock.scroll(row)
        await flush()
        assert.match(ui.node.innerHTML, new RegExp(`-data-gallery-idx="${(row - 1) * 2}"`))
        assert.match(ui.node.innerHTML, /<time datetime="2024-01-/)
        assert.equal(thumbnailCount(ui), 12)
        assert.equal(renderedSprites(ui).size, 0)
        for (const tile of ui.node.querySelectorAll('.thumbnail')) {
            assert.equal(tile.style.backgroundColor, '#ABC')
            assert.equal(tile.style.backgroundImage, undefined)
        }
        assert.equal(ui.imageRequests.length, initialRequests)
        assert.equal(clock.pending, 1, 'continuous scroll resets the single idle timer')
    }
    assert.equal(ui.requests.filter(path => path.endsWith('/entry_index.json')).length, initialIndexRequests,
        'cached entry data needs no new fetch or month conversion')
    for (const image of oldOriginals) image.onload()
    await flush()
    assert.equal(ui.imageRequests.length, initialRequests, 'old completions cannot start queued upgrades')
    await clock.settle()
    assert.ok(renderedSprites(ui).size > 0, 'unchanged card window receives images at idle')
    assert.ok(ui.imageRequests.length > initialRequests)
    assert.equal(originalRequests(ui).length, oldOriginals.length + 4)
})

test('scroll bursts coalesce into one animation frame for the latest viewport', async () => {
    const ui = gallery(false, [], manyEntries(200))
    ui.resolveClassifications({})
    await flush()
    const frames = []
    const animationFrame = ui.context.requestAnimationFrame
    ui.context.requestAnimationFrame = callback => frames.push(callback)
    const clock = scrollClock(ui)
    const initialHTML = ui.node.innerHTML
    for (const row of [14, 20, 30]) clock.scroll(row)
    assert.equal(frames.length, 1)
    assert.equal(ui.node.innerHTML, initialHTML)
    frames.shift()()
    await flush()
    assert.match(ui.node.innerHTML, /-data-gallery-idx="58"/)
    assert.doesNotMatch(ui.node.innerHTML, /-data-gallery-idx="26"/)
    ui.context.requestAnimationFrame = animationFrame
    await clock.settle()
})

for (const filtered of [false, true]) {
    test(`loaded ${filtered ? 'filtered originals' : 'previews and originals'} remain visible during scroll`, async () => {
        const entries = manyEntries(200)
        const ui = gallery(false, [], entries)
        ui.resolveClassifications(Object.fromEntries(entries.map(entry => [
            `2024/01/${entry.name}`, { tags: ['matching'], template: null },
        ])))
        await flush()
        if (filtered) await applyFilters(ui, { 'filter-search': 'matching' })
        // Complete one image only; the remaining tiles keep their loaded previews.
        const loaded = originalRequests(ui)[0]
        loaded.onload()
        await flush()
        const clock = scrollClock(ui)
        const initialRequests = ui.imageRequests.length
        clock.scroll(0)
        await flush()
        const tile = ui.node.querySelectorAll('.thumbnail').find(node => node.getAttribute('href') === loaded.src)
        assert.equal(tile.style.backgroundImage, `url('${loaded.src}')`)
        if (!filtered) assert.ok(renderedSprites(ui).size > 0)
        assert.equal(ui.imageRequests.length, initialRequests)
        await clock.settle()
    })
}

test('filtered scroll defers direct originals, including metadata-triggered renders', async () => {
    const entries = manyEntries(200).map(entry => ({ ...entry, bg: 'DEF' }))
    let releaseText
    const textIndex = new Promise(resolve => { releaseText = resolve })
    const ui = gallery(false, [], entries, 'localhost', { textIndex })
    ui.resolveClassifications(Object.fromEntries(entries.map(entry => [
        `2024/01/${entry.name}`, { tags: ['matching'], template: null },
    ])))
    await flush()
    await applyFilters(ui, { 'filter-search': 'matching' })
    const clock = scrollClock(ui)
    const initialRequests = ui.imageRequests.length
    clock.scroll(20)
    await flush()
    releaseText({})
    await flush()
    assert.match(ui.node.innerHTML, /-data-gallery-idx="38"/)
    assert.match(ui.node.innerHTML, /<time datetime="2024-01-/)
    assert.doesNotMatch(ui.node.innerHTML, /background-image:/)
    assert.match(ui.node.innerHTML, /background-color: #DEF;/)
    assert.equal(ui.imageRequests.length, initialRequests)
    await clock.settle()
    assert.match(ui.node.innerHTML, /background-image: url\('https:\/\/archive.example/)
    assert.equal(renderedSprites(ui).size, 0)
    assert.equal(ui.imageRequests.length, initialRequests + 12)
})

test('uncached months show immediate shells, then dates/colors without images during scroll', async () => {
    const entries = manyEntries(200).map(entry => ({ ...entry, bg: 'FED' }))
    const ui = gallery(false, [], entries, 'localhost', {
        dirIndex: { '2024/01': 200, '2024/02': 200, '2024/03': 200, '2024/04': 200 },
    })
    ui.resolveClassifications({})
    await flush()
    const fetchJson = ui.context.fetchJson
    let release
    const month = new Promise(resolve => { release = resolve })
    ui.context.fetchJson = path => path === 'images/2024/02/entry_index.json' ? month : fetchJson(path)
    const clock = scrollClock(ui)
    const initialRequests = ui.imageRequests.length
    clock.scroll(210)
    await flush()
    assert.match(ui.node.innerHTML, /aria-busy="true"/)
    assert.match(ui.node.innerHTML, /Datum wird geladen/)
    assert.match(ui.node.innerHTML, /top: 63745px;/)
    assert.equal(thumbnailCount(ui), 12)
    assert.equal(ui.imageRequests.length, initialRequests)
    release(entries)
    await flush()
    assert.doesNotMatch(ui.node.innerHTML, /aria-busy="true"/)
    assert.match(ui.node.innerHTML, /-data-gallery-idx="418"/)
    assert.match(ui.node.innerHTML, /<time datetime="2024-01-/)
    assert.match(ui.node.innerHTML, /background-color: #FED;/)
    assert.doesNotMatch(ui.node.innerHTML, /background-image:/)
    assert.equal(ui.imageRequests.length, initialRequests)
    await clock.settle()
    assert.ok(renderedSprites(ui).size > 0)
})

test('late month responses cannot replace a newer scroll viewport', async () => {
    const entries = manyEntries(200)
    const ui = gallery(false, [], entries, 'localhost', {
        dirIndex: { '2024/01': 200, '2024/02': 200, '2024/03': 200, '2024/04': 200 },
    })
    ui.resolveClassifications({})
    await flush()
    const fetchJson = ui.context.fetchJson
    let release
    const month = new Promise(resolve => { release = resolve })
    ui.context.fetchJson = path => path === 'images/2024/02/entry_index.json' ? month : fetchJson(path)
    const clock = scrollClock(ui)
    const initialRequests = ui.imageRequests.length
    clock.scroll(210)
    await flush()
    clock.scroll(20)
    await flush()
    const latest = ui.node.innerHTML
    release(entries)
    await flush()
    assert.equal(ui.node.innerHTML, latest)
    assert.match(latest, /-data-gallery-idx="38"/)
    assert.equal(ui.imageRequests.length, initialRequests)
    await clock.settle()
})

test('a filter change cancels pending scroll hydration and queued upgrades', async () => {
    const ui = gallery(false, [], manyEntries(200), 'localhost', {
        dirIndex: { '2024/01': 200, '2024/02': 200, '2024/03': 200, '2024/04': 200 },
    })
    ui.resolveClassifications({})
    await flush()
    const fetchJson = ui.context.fetchJson
    let release
    const month = new Promise(resolve => { release = resolve })
    ui.context.fetchJson = path => path === 'images/2024/02/entry_index.json' ? month : fetchJson(path)
    const clock = scrollClock(ui)
    const initialRequests = ui.imageRequests.length
    clock.scroll(210)
    await flush()
    ui.controls['filter-reactions'].value = '999'
    ui.controls['filter-reactions'].handler()
    await clock.settle()
    release(manyEntries(200))
    for (const image of originalRequests(ui)) image.onload()
    await flush()
    assert.equal(ui.node.innerHTML, '')
    assert.equal(ui.controls['filter-status'].textContent, '0 passende Bilder')
    assert.equal(ui.imageRequests.length, initialRequests)
})

test('renders viewport rows plus one-row overscan and refreshes within a month', async () => {
    const ui = gallery(false, [], manyEntries(200))
    await flush()
    assert.equal(thumbnailCount(ui), 10)
    assert.deepEqual([...renderedSprites(ui)], ['images/2024/01/thumbnails-09.webp'])
    ui.resolveClassifications({})
    await flush()
    ui.context.document.documentElement.scrollTop = 14 * 305
    await updateViewport(ui)
    assert.equal(thumbnailCount(ui), 12)
    assert.doesNotMatch(ui.node.innerHTML, /-data-gallery-idx="0"/)
    assert.match(ui.node.innerHTML, /-data-gallery-idx="26"/)
    assert.deepEqual([...renderedSprites(ui)], ['images/2024/01/thumbnails-08.webp'])
    ui.context.document.documentElement.scrollTop = 20 * 305
    await updateViewport(ui)
    assert.deepEqual([...renderedSprites(ui)].sort(), ['images/2024/01/thumbnails-07.webp', 'images/2024/01/thumbnails-08.webp'])
})

test('twenty visible images need at most three sprites, including a partial newest sheet', async () => {
    const ui = gallery(false, [], manyEntries(201), 'localhost', { clientWidth: 5 * 228 })
    await flush()
    assert.equal(thumbnailCount(ui), 25)
    assert.equal(renderedSprites(ui).size, 3)
    ui.resolveClassifications({})
    await flush()
    assert.equal(renderedSprites(ui).size, 3)
    ui.context.document.documentElement.scrollTop = 10 * 305
    await updateViewport(ui)
    assert.equal(thumbnailCount(ui), 30)
    assert.ok(renderedSprites(ui).size <= 3)
})

test('window accounts for the gallery offset and changes when only viewport height changes', async () => {
    const ui = gallery(false, [], manyEntries(200), 'localhost', {
        clientWidth: 5 * 228, galleryTop: 2 * 305,
    })
    await flush()
    assert.equal(thumbnailCount(ui), 15)
    ui.context.innerHeight = 6 * 305
    await updateViewport(ui, 'resize')
    assert.equal(thumbnailCount(ui), 25)
    ui.resolveClassifications({})
    await flush()
})

test('filtered results load originals in the same viewport-sized window', async () => {
    const entries = manyEntries(201)
    const ui = gallery(false, [], entries, 'localhost', { clientWidth: 5 * 228 })
    ui.resolveClassifications(Object.fromEntries(entries.map(entry => [
        `2024/01/${entry.name}`, { tags: ['matching'], template: null },
    ])))
    await flush()
    await applyFilters(ui, { 'filter-search': 'matching' })
    const renderedOriginals = () => [...ui.node.innerHTML.matchAll(/background-image: url\('https:\/\/archive.example\/images\/[^']+'/g)]
    assert.equal(thumbnailCount(ui), 25)
    assert.equal(renderedOriginals().length, 25)
    assert.equal(renderedSprites(ui).size, 0)
    ui.context.document.documentElement.scrollTop = 10 * 305
    await updateViewport(ui)
    assert.equal(thumbnailCount(ui), 30)
    assert.equal(renderedOriginals().length, 30)
    assert.equal(renderedSprites(ui).size, 0)
    assert.doesNotMatch(ui.node.innerHTML, /-data-gallery-idx="0"/)
    assert.match(ui.node.innerHTML, /-data-gallery-idx="45"/)
})

for (const [control, value] of [
    ['filter-search', 'Sesamstraße'],
    ['filter-template', result.template],
    ['filter-reactions', '7'],
]) {
    test(`${control} switches to originals and clearing it restores sprites`, async () => {
        const ui = gallery()
        ui.resolveClassifications({ [imageId]: result })
        await flush()
        const unfilteredHTML = ui.node.innerHTML
        assert.equal(renderedSprites(ui).size, 1)
        await applyFilters(ui, { [control]: value })
        assert.equal(thumbnailCount(ui), 1)
        assert.equal(renderedSprites(ui).size, 0)
        assert.match(ui.node.innerHTML, /background-image: url\('https:\/\/archive.example\/images\/2024\/01\/2024-01-02_b.jpg'\);/)
        assert.match(ui.node.innerHTML, /background-position: center; background-size: contain; background-color: #000;/)
        const item = openGalleryItem(ui, 0)
        assert.equal(item.src, 'https://archive.example/images/2024/01/2024-01-02_b.jpg')
        await applyFilters(ui, { [control]: control === 'filter-reactions' ? '0' : '' })
        assert.equal(ui.node.innerHTML, unfilteredHTML)
    })
}

const originalRequests = ui => ui.imageRequests.filter(image => image.src.startsWith('https://'))
const spriteRequests = ui => ui.imageRequests.filter(image => image.src.startsWith('images/'))

test('unsupported WebP renders archive originals without requesting sprite sheets', async () => {
    const ui = gallery(false, [], manyEntries(200), 'localhost', { webpSupported: false })
    await flush()
    assert.equal(renderedSprites(ui).size, 0)
    assert.match(ui.node.innerHTML, /background-size: contain; background-color: #000;/)
    assert.equal(thumbnailCount(ui), 10)
    assert.equal(spriteRequests(ui).length, 0)
    originalRequests(ui)[0].onload()
    ui.resolveClassifications({})
    await flush()
    assert.equal(renderedSprites(ui).size, 0)
    assert.equal(spriteRequests(ui).length, 0)
})

test('original upgrades wait for sprite load and paint, then wait for original decoding', async () => {
    const frames = []
    let finishDecode
    const ui = gallery(false, [], null, 'localhost', {
        requestAnimationFrame: callback => frames.push(callback),
        imageLoad() {},
        decode: (image, src) => src.startsWith('images/') ? Promise.resolve() :
            new Promise(resolve => { finishDecode = resolve }),
    })
    await flush()
    assert.equal(spriteRequests(ui).length, 1, 'sprite and metadata share one image load')
    assert.equal(originalRequests(ui).length, 0)
    assert.equal(frames.length, 0)
    spriteRequests(ui)[0].onload()
    await flush()
    assert.equal(originalRequests(ui).length, 0)
    frames.shift()()
    await flush()
    assert.equal(originalRequests(ui).length, 0)
    frames.shift()()
    await flush()
    assert.equal(originalRequests(ui).length, 2)
    const original = originalRequests(ui)[0]
    assert.equal(original.fetchPriority, 'low')
    assert.equal(original.decoding, 'async')
    original.onload()
    await flush()
    assert.equal(renderedSprites(ui).size, 1, 'keep preview until decode completes')
    finishDecode()
    await flush()
    const tile = ui.node.querySelectorAll('.thumbnail').find(node => node.getAttribute('href') === original.src)
    assert.equal(tile.style.backgroundImage, `url('${original.src}')`)
    assert.equal(tile.style.backgroundPosition, 'center')
    assert.equal(tile.style.backgroundSize, 'contain')
    assert.equal(tile.style.backgroundColor, '#000')
    assert.equal(renderedSprites(ui).size, 1, 'other tile still shows its preview')
    ui.resolveClassifications({})
    await flush()
})

test('entry bg is retained from sprite previews through original upgrades and filters', async () => {
    const entries = [
        { name: '2024-01-01_a.jpg', w: 500, h: 400, bg: 'ABC' },
        { name: '2024-01-02_b.jpg', w: 500, h: 400, bg: 'FFF' },
    ]
    const ui = gallery(false, [], entries)
    await flush()
    const colors = () => ui.node.querySelectorAll('.thumbnail').map(tile => tile.style.backgroundColor)
    assert.deepEqual(colors(), ['#FFF', '#ABC'])
    for (const image of originalRequests(ui)) image.onload()
    await flush()
    assert.equal(renderedSprites(ui).size, 0)
    assert.deepEqual(colors(), ['#FFF', '#ABC'])
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    await applyFilters(ui, { 'filter-search': 'Sesamstraße' })
    assert.deepEqual(colors(), ['#FFF'])
    await applyFilters(ui, { 'filter-search': '' })
    assert.deepEqual(colors(), ['#FFF', '#ABC'])
})

test('missing or malformed entry bg falls back to black', async () => {
    const entries = [undefined, null, 123, '#FFF', 'FFFFFF', 'FFF; color: red'].map((bg, index) => ({
        name: `2024-01-01_${index}.jpg`, w: 500, h: 400, bg,
    }))
    const ui = gallery(false, [], entries, 'localhost', { webpSupported: false })
    await flush()
    for (const tile of ui.node.querySelectorAll('.thumbnail')) {
        assert.equal(tile.style.backgroundColor, '#000')
    }
    ui.resolveClassifications({})
    await flush()
})

test('loaded originals survive metadata renders, filters and subsequent scrolling', async () => {
    const ui = gallery()
    await flush()
    const originals = [...originalRequests(ui)]
    for (const image of originals) image.onload()
    await flush()
    assert.equal(renderedSprites(ui).size, 0)
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    assert.equal(renderedSprites(ui).size, 0)
    await applyFilters(ui, { 'filter-search': 'OCR' })
    await applyFilters(ui, { 'filter-search': '' })
    await updateViewport(ui)
    assert.equal(renderedSprites(ui).size, 0)
    assert.equal(originalRequests(ui).length, originals.length, 'no duplicate original loads')
})

test('failed original loads keep their preview and do not loop or downgrade other tiles', async () => {
    const ui = gallery()
    await flush()
    const [failed, loaded] = originalRequests(ui)
    failed.onerror()
    loaded.onload()
    await flush()
    ui.resolveClassifications({})
    await flush()
    assert.equal(originalRequests(ui).length, 2)
    const tiles = ui.node.querySelectorAll('.thumbnail')
    assert.match(tiles.find(node => node.getAttribute('href') === failed.src).style.backgroundImage, /\.webp/)
    assert.equal(tiles.find(node => node.getAttribute('href') === loaded.src).style.backgroundImage, `url('${loaded.src}')`)
})

test('missing sprite sheets fall back to originals and remain optional on rerender', async () => {
    const ui = gallery(false, [], null, 'localhost', {
        imageLoad: (image, src) => { if (src.startsWith('images/')) image.onerror() },
    })
    await flush()
    assert.equal(renderedSprites(ui).size, 0)
    assert.match(ui.node.innerHTML, /background-image: url\('https:\/\/archive.example/)
    assert.equal(spriteRequests(ui).length, 1)
    for (const image of originalRequests(ui)) image.onload()
    ui.resolveClassifications({})
    await flush()
    assert.equal(renderedSprites(ui).size, 0)
    assert.equal(spriteRequests(ui).length, 1)
})

test('upgrades use at most four slots and discard queued images outside the new window', async () => {
    const ui = gallery(false, [], manyEntries(200))
    await flush()
    assert.equal(originalRequests(ui).length, 4)
    assert.equal(spriteRequests(ui).length, 1)
    const old = [...originalRequests(ui)]
    ui.context.document.documentElement.scrollTop = 20 * 305
    await updateViewport(ui)
    assert.equal(originalRequests(ui).length, 4, 'old in-flight requests still occupy their slots')
    const before = ui.node.innerHTML
    old[0].onload()
    await flush()
    assert.equal(originalRequests(ui).length, 5)
    assert.equal(ui.node.innerHTML, before, 'an old completion must not mutate new tiles')
    const next = originalRequests(ui).at(-1)
    assert.match(next.src, /2024-01-16\d\.jpg$/)
    next.onload()
    await flush()
    assert.equal(originalRequests(ui).length, 6)
    assert.notEqual(ui.node.innerHTML, before)
    ui.resolveClassifications({})
    await flush()
})

test('changing reaction filters clears queued upgrades even before the debounced render', async () => {
    const ui = gallery(false, [], manyEntries(200))
    await flush()
    const originals = [...originalRequests(ui)]
    assert.equal(originals.length, 4)
    ui.controls['filter-reactions'].value = '999'
    ui.controls['filter-reactions'].handler()
    for (const image of originals) image.onload()
    await flush()
    assert.equal(originalRequests(ui).length, 4)
    assert.equal(ui.node.innerHTML, '')
    ui.resolveClassifications({})
    await new Promise(resolve => setTimeout(resolve, 180))
    await flush()
    assert.equal(originalRequests(ui).length, 4)
})

test('each visible sheet gates its own upgrades, without fetching non-rendered sheets', async () => {
    const ui = gallery(false, [], manyEntries(201), 'localhost', {
        clientWidth: 5 * 228, imageLoad() {},
    })
    await flush()
    assert.equal(spriteRequests(ui).length, 3)
    assert.equal(originalRequests(ui).length, 0)
    const partial = spriteRequests(ui).find(image => image.src.includes('thumbnails-10.webp'))
    partial.onload()
    await flush()
    assert.equal(originalRequests(ui).length, 1, 'partial sheet contains only the newest image')
    spriteRequests(ui).find(image => image.src.includes('thumbnails-09.webp')).onload()
    await flush()
    assert.equal(originalRequests(ui).length, 4)
    assert.equal(spriteRequests(ui).length, 3)
    ui.resolveClassifications({})
    await flush()
})

test('gallery uses six columns when wide enough and fewer columns after resizing', async () => {
    const ui = gallery(false, [], manyEntries(201), 'localhost', { clientWidth: 6 * 228 })
    await flush()
    assert.equal(thumbnailCount(ui), 30)
    assert.equal(ui.node.style.height, `${Math.ceil(201 / 6) * 305}px`)
    ui.resolveClassifications({})
    await flush()

    ui.node.clientWidth = 6 * 228 - 1
    await updateViewport(ui, 'resize')
    assert.equal(thumbnailCount(ui), 25)
    assert.equal(ui.node.style.height, `${Math.ceil(201 / 5) * 305}px`)
})

test('filters have stable control sizes and a two-row mobile layout before classifications load', () => {
    const style = readFileSync(`${__dirname}/../assets/style.css`, 'utf8')
    const template = readFileSync(`${__dirname}/../templates/index.html`, 'utf8')
    assert.match(template, /<label class="filter-template">\s*<select id="filter-template"[^>]*disabled>/)
    assert.match(style, /#gallery-filters \.filter-template \{\s*width: 170px;\s*flex: 0 0 170px;/,
        'option text must not determine the desktop wrapper width')
    assert.match(style, /#gallery-filters input,\s*#gallery-filters select \{ height: 38px; \}/)
    const mobile = style.slice(style.indexOf('@media screen and (max-width: 600px)'))
    assert.match(mobile, /#gallery-filters \{\s*display: grid;\s*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);\s*gap: 8px;/)
    assert.match(mobile, /#gallery-filters \.filter-reactions,\s*#gallery-filters \.filter-template \{\s*width: 100%;/)
    assert.match(mobile, /#gallery-filters \.filter-search,\s*#filter-status \{\s*grid-column: 1 \/ -1;/)
    assert.match(style, /#filter-template \{ max-width: 170px; \}/)
    assert.match(style, /#filter-status:empty \{ display: none; \}/, 'empty status must not create a third row')
})

test('220 px tiles and gallery geometry support six columns at full width', () => {
    const style = readFileSync(`${__dirname}/../assets/style.css`, 'utf8')
    assert.match(style, /\.thumbnail \{[^}]*width: var\(--thumbnail-display-size, 220px\);\s*height: var\(--thumbnail-display-size, 220px\);/)
    const card = style.match(/\.gallery-item \{([^}]+)\}/)[1]
    assert.match(card, /width: var\(--thumbnail-display-size, 220px\);\s*height: var\(--gallery-card-height, 290px\);\s*border-radius: 5px;\s*overflow: clip;/)
    assert.doesNotMatch(card, /background(?:-color)?:/, 'cards have no background color')
    assert.doesNotMatch(card, /(?:margin|padding):/, 'JS layout supplies the spacing')
    assert.match(style, /\.thumbnail-classification \{[^}]*height: 39px;/)
    assert.match(style, /\.thumbnail-metadata \{[^}]*padding-top: 1px;[^}]*line-height: 24px;/)
    assert.match(style, /\.classification-tags \{[^}]*line-height: 18px;/)
    assert.match(style, /\.classification-tag \{[^}]*background: transparent;/)
    assert.equal(305 - 290, 15, 'vertical gap')
    assert.equal(228 - 220, 8, 'horizontal gap')
    const containerWidth = Number(style.match(/\.container \{[^}]*max-width: (\d+)px;/)[1])
    assert.equal(containerWidth - 80, 6 * 240, 'six cards with maximum gaps fit inside the gallery margins')
})

test('absolute card positions supply gaps without CSS margins or padding', async () => {
    const ui = gallery(false, [], manyEntries(6), 'localhost', { clientWidth: 456 })
    await flush()
    const positions = [...ui.node.innerHTML.matchAll(/<article class="gallery-item" style="top: ([\d.]+)px; left: ([\d.]+)px;/g)]
        .map(([, top, left]) => [Number(top), Number(left)])
    assert.deepEqual(positions, [[0, 4], [0, 232], [305, 4], [305, 232], [610, 4], [610, 232]])
    assert.equal(positions[1][1] - positions[0][1] - 220, 8)
    assert.equal(positions[2][0] - positions[0][0] - 290, 15)
    assert.equal(ui.node.style.height, '915px')
    ui.resolveClassifications({})
    await flush()
})

test('horizontal gaps grow from 8px to 20px without sacrificing columns, with excess width centered', async () => {
    const lefts = ui => [...ui.node.innerHTML.matchAll(/<article class="gallery-item" style="top: 0px; left: ([\d.]+)px;/g)]
        .map(([, left]) => Number(left))
    for (const [width, expected] of [
        [456, [4, 232]],       // Minimum 8px gap.
        [466, [6.5, 239.5]],   // 13px gap using all available width.
        [480, [10, 250]],      // Maximum 20px gap.
        [500, [20, 260]],      // Cap the gap, then center the remainder.
        [684, [4, 232, 460]],  // A third column wins over larger gaps.
    ]) {
        const ui = gallery(false, [], manyEntries(6), 'localhost', { clientWidth: width })
        await flush()
        assert.deepEqual(lefts(ui), expected, `gallery width ${width}`)
        ui.resolveClassifications({})
        await flush()
    }
    const ui = gallery(false, [], manyEntries(6), 'localhost', { clientWidth: 466 })
    await flush()
    ui.resolveClassifications({})
    await flush()
    ui.node.clientWidth = 468
    await updateViewport(ui, 'resize')
    assert.deepEqual(lefts(ui), [7, 241], 'small resizes within the same column count still reposition cards')
})

test('full-width six-column gallery uses 20px horizontal gaps and 15px vertical gaps', async () => {
    const ui = gallery(false, [], manyEntries(12), 'localhost', { clientWidth: 1440 })
    await flush()
    const positions = [...ui.node.innerHTML.matchAll(/<article class="gallery-item" style="top: ([\d.]+)px; left: ([\d.]+)px;/g)]
        .map(([, top, left]) => [Number(top), Number(left)])
    assert.deepEqual(positions.slice(0, 6), [10, 250, 490, 730, 970, 1210].map(left => [0, left]))
    assert.equal(positions[1][1] - positions[0][1] - 220, 20)
    assert.equal(positions[6][0] - positions[0][0] - 290, 15)
    assert.equal(ui.node.style.height, '610px')
    ui.resolveClassifications({})
    await flush()
})

const cardPositions = ui => [...ui.node.innerHTML.matchAll(/<article class="gallery-item" style="top: ([\d.]+)px; left: ([\d.]+)px;/g)]
    .map(([, top, left]) => [Number(top), Number(left)])

test('iPhone SE viewport fits two 180px thumbnails with a 7px gap and 4px outer gutters', async () => {
    const ui = gallery(false, [], manyEntries(41), 'localhost', {
        clientWidth: 375, innerWidth: 375, innerHeight: 667, galleryTop: 130,
    })
    await flush()
    assert.equal(ui.node.style['--thumbnail-display-size'], '180px')
    assert.equal(ui.node.style['--gallery-card-height'], '250px')
    assert.deepEqual(cardPositions(ui).slice(0, 4), [[0, 4], [0, 191], [265, 4], [265, 191]])
    assert.equal(375 - 191 - 180, 4, 'right gutter')
    assert.equal(ui.node.style.height, `${21 * 265}px`)
    assert.equal(thumbnailCount(ui), 8, 'shorter rows render more cards in the same viewport')
    const tiles = ui.node.querySelectorAll('.thumbnail')
    const scale = 180 / 220
    for (const tile of tiles) {
        assert.ok(Math.abs(parseFloat(tile.style.backgroundSize) - 1108 * scale) < 1e-9)
        assert.match(tile.style.backgroundSize, /px auto$/, 'partial sheets use their natural aspect ratio')
    }
    assert.match(tiles[0].style.backgroundImage, /thumbnails-02\.webp/, 'reuse the partial newest sheet')
    assert.equal(tiles[0].style.backgroundPosition, '-0px -0px')
    const offsets = tiles[1].style.backgroundPosition.split(' ').map(parseFloat)
    assert.ok(Math.abs(offsets[0] + 888 * scale) < 1e-9)
    assert.ok(Math.abs(offsets[1] + 666 * scale) < 1e-9)
    ui.resolveClassifications({})
    await flush()
    ui.context.document.documentElement.scrollTop = 130 + 10 * 265
    await updateViewport(ui)
    assert.equal(thumbnailCount(ui), 10)
    assert.match(ui.node.innerHTML, /-data-gallery-idx="18"/)
    assert.doesNotMatch(ui.node.innerHTML, /-data-gallery-idx="0"/)
    assert.equal(cardPositions(ui)[0][0], 9 * 265)
})

test('compact widths use whole-pixel thumbnails and centered gutters, falling back below 320px', async () => {
    for (const [width, size, columns, gap] of [
        [200, 192, 1, 7], [319, 220, 1, 7], [320, 152, 2, 7], [360, 172, 2, 7],
        [375, 180, 2, 7], [376, 180, 2, 7], [390, 187, 2, 7], [414, 199, 2, 7],
        [455, 220, 2, 7], [456, 220, 2, 8],
    ]) {
        const ui = gallery(false, [], manyEntries(6), 'localhost', { clientWidth: width })
        await flush()
        assert.equal(ui.node.style['--thumbnail-display-size'], `${size}px`, `width ${width}`)
        const firstRow = cardPositions(ui).filter(([top]) => top === 0)
        assert.equal(firstRow.length, columns, `width ${width}`)
        const left = firstRow[0][1]
        const right = width - firstRow.at(-1)[1] - size
        assert.equal(left, right, 'center leftover space after rounding')
        assert.ok(left >= 4)
        if (columns === 2) assert.equal(firstRow[1][1] - left - size, gap)
        const cardHeight = size + 70
        const rowHeight = cardHeight + 15
        assert.equal(ui.node.style['--gallery-card-height'], `${cardHeight}px`)
        assert.equal(ui.node.style.height, `${Math.ceil(6 / columns) * rowHeight}px`)
        const positions = cardPositions(ui)
        assert.equal(positions[columns][0] - positions[0][0] - cardHeight, 15)
        ui.resolveClassifications({})
        await flush()
    }
})

test('resizing between compact and full-size cards updates positions and sprite scale without stale styles', async () => {
    const ui = gallery(false, [], manyEntries(6), 'localhost', { clientWidth: 375 })
    await flush()
    ui.resolveClassifications({})
    await flush()
    for (const [width, size, lefts] of [
        [376, 180, [4.5, 191.5]], [456, 220, [4, 232]], [375, 180, [4, 191]],
    ]) {
        ui.node.clientWidth = width
        await updateViewport(ui, 'resize')
        assert.equal(ui.node.style['--thumbnail-display-size'], `${size}px`)
        assert.deepEqual(cardPositions(ui).slice(0, 2).map(([, left]) => left), lefts)
        const tile = ui.node.querySelectorAll('.thumbnail')[0]
        if (size === 220) {
            assert.equal(tile.style.backgroundSize, undefined, 'full-size sprite uses its natural dimensions')
            assert.equal(tile.style.backgroundPosition, '-0px -222px')
        } else {
            assert.ok(Math.abs(parseFloat(tile.style.backgroundSize) - 1108 * size / 220) < 1e-9)
        }
        assert.equal(ui.node.style['--gallery-card-height'], `${size + 70}px`)
        assert.equal(ui.node.style.height, `${3 * (size + 85)}px`, 'row spacing follows thumbnail size')
        assert.equal(cardPositions(ui)[2][0], size + 85)
    }
})

test('resizing a scrolled mobile gallery recalculates its visible window using the new row height', async () => {
    const ui = gallery(false, [], manyEntries(100), 'localhost', { clientWidth: 375, innerHeight: 667 })
    await flush()
    ui.resolveClassifications({})
    await flush()
    ui.context.document.documentElement.scrollTop = 10 * 265
    await updateViewport(ui)
    assert.match(ui.node.innerHTML, /-data-gallery-idx="18"/)
    assert.equal(cardPositions(ui)[0][0], 9 * 265)
    ui.node.clientWidth = 414
    await updateViewport(ui, 'resize')
    assert.equal(ui.node.style['--thumbnail-display-size'], '199px')
    assert.equal(ui.node.style['--gallery-card-height'], '269px')
    assert.equal(ui.node.style.height, `${50 * 284}px`)
    assert.match(ui.node.innerHTML, /-data-gallery-idx="16"/)
    assert.equal(cardPositions(ui)[0][0], 8 * 284)
    assert.equal(cardPositions(ui)[2][0] - cardPositions(ui)[0][0] - 269, 15)
})

test('compact original upgrades, filtering and failed sprites keep contain sizing instead of sprite scaling', async () => {
    const ui = gallery(false, [], null, 'localhost', { clientWidth: 375 })
    await flush()
    const original = originalRequests(ui)[0]
    original.onload()
    await flush()
    const upgraded = ui.node.querySelectorAll('.thumbnail').find(tile => tile.getAttribute('href') === original.src)
    assert.equal(upgraded.style.backgroundSize, 'contain')
    assert.equal(upgraded.style.backgroundPosition, 'center')
    assert.equal(ui.node.style['--thumbnail-display-size'], '180px')
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    await applyFilters(ui, { 'filter-search': 'Sesamstraße' })
    assert.equal(thumbnailCount(ui), 1)
    assert.equal(ui.node.querySelectorAll('.thumbnail')[0].style.backgroundSize, 'contain')
    assert.equal(ui.node.style['--thumbnail-display-size'], '180px')
    assert.equal(ui.node.style['--gallery-card-height'], '250px')
    assert.equal(ui.node.style.height, '265px')

    for (const options of [{ webpSupported: false }, { imageLoad(image, src) {
        if (src.startsWith('images/')) image.onerror()
    } }]) {
        const fallback = gallery(false, [], null, 'localhost', { clientWidth: 375, ...options })
        await flush()
        assert.equal(fallback.node.style['--thumbnail-display-size'], '180px')
        assert.ok(fallback.node.querySelectorAll('.thumbnail').every(tile => tile.style.backgroundSize === 'contain'))
        fallback.resolveClassifications({})
        await flush()
    }
})

function urlParams(ui) {
    return new URL(ui.context.location.href).searchParams
}

function clickThumbnail(ui, index = 0) {
    ui.listeners.click[0]({
        target: {
            classList: { contains: name => name === 'thumbnail' },
            getAttribute: () => String(index),
        },
        preventDefault() {},
    })
}

test('URLs capture combined filters, preserve unrelated parameters and omit defaults', async () => {
    const ui = gallery(false, [], null, 'localhost', { query: '?campaign=test#gallery' })
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    await applyFilters(ui, {
        'filter-search': 'Sesamstraße & Bert', 'filter-template': result.template, 'filter-reactions': '7',
    })
    assert.equal(urlParams(ui).get('q'), 'Sesamstraße & Bert')
    assert.equal(urlParams(ui).get('template'), result.template)
    assert.equal(urlParams(ui).get('reactions'), '7')
    assert.equal(urlParams(ui).get('campaign'), 'test')
    assert.equal(new URL(ui.context.location.href).hash, '#gallery')
    await applyFilters(ui, { 'filter-search': '', 'filter-template': '', 'filter-reactions': '0' })
    assert.equal(ui.context.location.href, 'http://localhost/?campaign=test#gallery')
})

test('shared filtered image waits for metadata and keeps a rare template selected', async () => {
    let releaseTelegram, releaseText
    const telegram = new Promise(resolve => { releaseTelegram = resolve })
    const textIndex = new Promise(resolve => { releaseText = resolve })
    const params = new URLSearchParams({ q: 'OCR', template: result.template, reactions: '7', image: imageId })
    const ui = gallery(false, [], null, 'localhost', { query: `?${params}`, telegram, textIndex })
    await flush()
    assert.equal(ui.controls['filter-search'].value, 'OCR')
    assert.equal(ui.controls['filter-template'].value, result.template)
    assert.equal(ui.lightbox.openedIndex, undefined)
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    assert.equal(ui.controls['filter-template'].value, result.template)
    assert.ok(ui.controls['filter-template'].options.some(option => option.value === result.template))
    releaseTelegram({ '2024-01-02_b.jpg': [42, 7, 100, 2] })
    await flush()
    assert.equal(ui.lightbox.openedIndex, undefined)
    releaseText({ [imageId]: { text: result.text, description: result.description } })
    await flush()
    assert.equal(ui.lightbox.openedIndex, 0)
    assert.equal(ui.lightbox.options.dataSource[0].imageId, imageId)
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
})

test('shared unfiltered image loads its month and uses a stable filename, not a positional index', async () => {
    const ui = gallery(true, [], null, 'localhost', {
        query: '?image=2024%2F01%2F2024-01-01_a.jpg',
    })
    ui.resolveClassifications({})
    await flush()
    await flush()
    assert.equal(ui.lightbox.openedIndex, 5, 'four newer images must not change the linked image')
    assert.equal(ui.lightbox.options.dataSource[5].imageId, '2024/01/2024-01-01_a.jpg')
    assert.ok(ui.requests.includes('images/2024/01/entry_index.json'))
})

test('opening, changing and closing lightbox images update URLs and support Back/Forward', async () => {
    const ui = gallery()
    ui.resolveClassifications({})
    await flush()
    clickThumbnail(ui)
    await flush()
    assert.equal(urlParams(ui).get('image'), imageId)
    assert.equal(ui.context.history.entries.length, 2)
    await ui.lightbox.goTo(1)
    assert.equal(urlParams(ui).get('image'), '2024/01/2024-01-01_a.jpg')
    assert.equal(ui.context.history.entries.length, 2, 'slide changes replace the lightbox entry')
    await ui.lightbox.close()
    assert.equal(urlParams(ui).get('image'), null)
    assert.equal(ui.context.history.entries.length, 3)
    await ui.context.history.go(-1)
    assert.equal(ui.lightbox.pswp.currIndex, 1)
    await ui.context.history.go(-1)
    assert.equal(ui.lightbox.pswp, undefined)
    assert.equal(urlParams(ui).get('image'), null)
    await ui.context.history.go(1)
    assert.equal(ui.lightbox.pswp.currIndex, 1)
    await ui.context.history.go(1)
    assert.equal(ui.lightbox.pswp, undefined)
    assert.equal(ui.context.history.entries.length, 3, 'restoring never adds history entries')
})

test('Back/Forward restore filters and cancel a pending search debounce', async () => {
    const ui = gallery()
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    await applyFilters(ui, { 'filter-search': 'OCR', 'filter-reactions': '7' })
    ui.controls['filter-search'].value = 'pending change'
    ui.controls['filter-search'].handler()
    await ui.context.history.go(-1)
    assert.equal(ui.controls['filter-search'].value, '')
    assert.equal(Number(ui.controls['filter-reactions'].value), 0)
    assert.match(ui.node.innerHTML, /2024-01-01_a.jpg/)
    await new Promise(resolve => setTimeout(resolve, 220))
    assert.equal(urlParams(ui).get('q'), null)
    await ui.context.history.go(1)
    assert.equal(ui.controls['filter-search'].value, 'OCR')
    assert.equal(Number(ui.controls['filter-reactions'].value), 7)
    assert.doesNotMatch(ui.node.innerHTML, /2024-01-01_a.jpg/)
    assert.equal(ui.context.history.entries.length, 2)
})

test('scroll position stays local to history and never appears in shared URLs', async () => {
    const entries = manyEntries(100)
    const ui = gallery(false, [], entries)
    ui.resolveClassifications({})
    await flush()
    ui.context.document.documentElement.scrollTop = 10 * 305
    for (const handler of ui.listeners.scroll) handler({ type: 'scroll', constructor: { name: 'Event' } })
    assert.equal(urlParams(ui).get('scroll'), null)
    assert.equal(ui.context.location.href, 'http://localhost/')
    assert.equal(ui.context.history.state.galleryScroll, 10 * 305)
    assert.equal(ui.context.history.entries.length, 1)
    const shared = gallery(false, [], entries, 'localhost', { query: new URL(ui.context.location.href).search })
    shared.resolveClassifications({})
    await flush()
    await flush()
    assert.equal(shared.context.document.documentElement.scrollTop, 0)
    assert.match(shared.node.innerHTML, /-data-gallery-idx="0"/)
    await applyFilters(ui, { 'filter-reactions': '1' })
    assert.equal(ui.context.document.documentElement.scrollTop, 0)
    // Model the browser clamping scroll against the currently rendered height.
    ui.context.scrollTo = (x, y) => {
        ui.context.document.documentElement.scrollTop = Math.min(y,
            Math.max(0, parseInt(ui.node.style.height) - ui.context.innerHeight))
    }
    await ui.context.history.go(-1)
    assert.equal(ui.context.document.documentElement.scrollTop, 10 * 305)
})

test('invalid numeric URL state and missing or excluded images degrade to usable gallery', async () => {
    for (const query of [
        '?reactions=Infinity&image=../missing.jpg',
        '?reactions=-7&image=2024/01/deleted.jpg',
        `?q=no-match&image=${encodeURIComponent(imageId)}`,
    ]) {
        const ui = gallery(false, [], null, 'localhost', { query })
        ui.resolveClassifications({ [imageId]: result })
        await flush()
        await flush()
        assert.equal(ui.lightbox.openedIndex, undefined)
        assert.equal(Number(ui.controls['filter-reactions'].value), 0)
        assert.equal(ui.context.document.documentElement.scrollTop, 0)
        assert.match(ui.controls['filter-status'].textContent, /Verlinktes Bild nicht verfügbar/)
        await applyFilters(ui, { 'filter-search': 'OCR' })
        assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
        assert.equal(urlParams(ui).get('image'), null)
    }
})

test('a superseded initial restoration cannot overwrite newer user filters', async () => {
    let releaseText
    const textIndex = new Promise(resolve => { releaseText = resolve })
    const ui = gallery(false, [], null, 'localhost', { query: `?q=OCR&image=${imageId}`, textIndex })
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    await applyFilters(ui, { 'filter-search': 'Sesamstraße' })
    releaseText({ [imageId]: { text: result.text } })
    await flush()
    assert.equal(ui.controls['filter-search'].value, 'Sesamstraße')
    assert.equal(urlParams(ui).get('q'), 'Sesamstraße')
    assert.equal(urlParams(ui).get('image'), null)
    assert.equal(ui.lightbox.openedIndex, undefined)
})

test('scrolling or opening an image never serializes unapplied search input', async () => {
    const ui = gallery()
    ui.resolveClassifications({})
    await flush()
    ui.controls['filter-search'].value = 'not applied yet'
    for (const handler of ui.listeners.scroll) handler({ type: 'scroll', constructor: { name: 'Event' } })
    clickThumbnail(ui)
    await flush()
    assert.equal(urlParams(ui).get('q'), null)
    assert.equal(urlParams(ui).get('image'), imageId)
})

test('Back during the lightbox opening animation waits for teardown without writing history', async () => {
    const ui = gallery()
    ui.resolveClassifications({})
    await flush()
    clickThumbnail(ui)
    await flush()
    const pswp = ui.lightbox.pswp
    pswp.opener.isOpen = false
    await ui.context.history.go(-1)
    assert.equal(ui.lightbox.pswp, pswp)
    pswp.opener.isOpen = true
    pswp.emit('openingAnimationEnd')
    await flush()
    assert.equal(ui.lightbox.pswp, undefined)
    assert.equal(urlParams(ui).get('image'), null)
    assert.equal(ui.context.history.entries.length, 2)
    await ui.context.history.go(1)
    assert.equal(ui.lightbox.pswp.currIndex, 0)
})

test('closing while a neighboring month is loading cannot resurrect the selected image URL', async () => {
    const ui = gallery(true)
    ui.resolveClassifications({})
    await flush()
    const originalFetch = ui.context.fetchJson
    let release
    const pendingMonth = new Promise(resolve => { release = resolve })
    ui.context.fetchJson = path => path === 'images/2024/01/entry_index.json' ? pendingMonth : originalFetch(path)
    clickThumbnail(ui)
    await flush()
    assert.equal(urlParams(ui).get('image'), '2024/03/2024-03-02_b.jpg')
    // PhotoSwipe remains attached during its closing animation.
    await ui.lightbox.emit('close')
    release(await originalFetch('images/2024/01/entry_index.json'))
    await flush()
    assert.equal(urlParams(ui).get('image'), null)
})

test('browsing requests only loaded months, filters expand to the full catalog without duplicate metadata requests', async () => {
    const ui = gallery(true)
    ui.resolveClassifications({
        '2024/01/2024-01-02_b.jpg': { ...result, text: 'older month only' },
        '2024/03/2024-03-02_b.jpg': result,
    })
    await flush()
    await flush()
    assert.ok(ui.requests.includes('images/classification_catalog.json'))
    for (const kind of ['telegram_metadata', 'classification_index', 'classification_text_index']) {
        assert.ok(ui.requests.includes(`images/2024/03/${kind}.json`))
        assert.ok(!ui.requests.includes(`images/2024/01/${kind}.json`))
        assert.ok(!ui.requests.includes(`images/${kind}.json`))
    }
    await applyFilters(ui, { 'filter-search': 'older month only' })
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.doesNotMatch(ui.node.innerHTML, /2024-03-02_b.jpg/)
    for (const kind of ['telegram_metadata', 'classification_index', 'classification_text_index']) {
        for (const month of ['01', '02', '03']) {
            assert.equal(ui.requests.filter(path => path === `images/2024/${month}/${kind}.json`).length, 1)
        }
    }
})

test('a failed metadata month keeps images and other chunks, and a filter change retries it', async () => {
    const ui = gallery(true)
    ui.resolveClassifications({ '2024/01/2024-01-02_b.jpg': { ...result, text: 'older month only' } })
    await flush()
    const fetchJson = ui.context.fetchJson
    const failedPath = 'images/2024/01/classification_text_index.json'
    let attempts = 0
    ui.context.fetchJson = path => {
        if (path === failedPath && attempts++ === 0) return Promise.reject(new Error('offline'))
        return fetchJson(path)
    }
    await applyFilters(ui, { 'filter-search': 'older month only' })
    assert.equal(ui.node.innerHTML, '')
    assert.match(ui.controls['filter-status'].textContent, /Archiv unvollständig/)
    await applyFilters(ui, { 'filter-search': 'older month only' })
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.equal(attempts, 2)
})

test('worker results from superseded queries cannot replace newer filters or cleared filters', async () => {
    const workers = [], held = []
    let oldId
    const ui = gallery(false, [], null, 'localhost', { worker: {
        instances: workers,
        deliver(data, deliver) {
            if (data.type === 'results') {
                oldId ??= data.id
                if (data.id === oldId) { held.push(deliver); return }
            }
            queueMicrotask(deliver)
        },
    } })
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.equal(ui.controls['filter-status'].textContent, 'Suche läuft…')
    assert.ok(held.length)
    await applyFilters(ui, { 'filter-search': 'absent' })
    assert.equal(ui.controls['filter-status'].textContent, '0 passende Bilder')
    for (const deliver of held) deliver()
    await flush()
    assert.equal(ui.node.innerHTML, '')
    assert.equal(ui.controls['filter-status'].textContent, '0 passende Bilder')
    const queryCount = () => workers[0].sent.filter(data => data.type === 'query').length
    assert.equal(queryCount(), 2)
    const clock = scrollClock(ui)
    clock.scroll(0)
    await clock.settle()
    assert.equal(queryCount(), 2, 'scrolling must not re-run archive filtering')
    ui.controls['filter-search-clear'].handler()
    await clock.settle()
    for (const deliver of held) deliver()
    await flush()
    assert.equal(thumbnailCount(ui), 2)
    assert.equal(ui.controls['filter-status'].textContent, '')
})

test('worker construction failure leaves browsing usable and reports unavailable search', async () => {
    const ui = gallery(false, [], null, 'localhost', { worker: { unavailable: true } })
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    assert.equal(thumbnailCount(ui), 2)
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.match(ui.controls['filter-status'].textContent, /Suche nicht verfügbar/)
    assert.equal(ui.node.innerHTML, '')
    await applyFilters(ui, { 'filter-search': '' })
    assert.equal(thumbnailCount(ui), 2)
})

test('asynchronous worker startup failure falls back to browsing without hanging initialization', async () => {
    const workers = []
    const ui = gallery(false, [], null, 'localhost', { worker: {
        instances: workers,
        deliver(data, deliver) {
            if (data.type !== 'fetched') queueMicrotask(deliver)
        },
    } })
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    workers[0].onerror({ message: 'Worker script blocked', preventDefault() {} })
    await flush()
    assert.equal(workers[0].terminated, true)
    assert.equal(thumbnailCount(ui), 2)
    assert.equal(ui.context.history.scrollRestoration, 'manual')
})

test('a worker crash during filtering settles the query and a new filter retries in a fresh worker', async () => {
    const workers = []
    let withhold = true
    const ui = gallery(false, [], null, 'localhost', { worker: {
        instances: workers,
        deliver(data, deliver) {
            if (withhold && data.type === 'results') return
            queueMicrotask(deliver)
        },
    } })
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.equal(ui.controls['filter-status'].textContent, 'Suche läuft…')
    workers[0].onerror({ message: 'Worker crashed', preventDefault() {} })
    await flush()
    assert.match(ui.controls['filter-status'].textContent, /Suche nicht verfügbar/)
    withhold = false
    await applyFilters(ui, { 'filter-search': 'OCR' })
    assert.equal(workers.length, 2)
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
})
