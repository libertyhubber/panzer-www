const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { test } = require('node:test')
const vm = require('node:vm')

const appSource = readFileSync(`${__dirname}/../assets/app.js`, 'utf8')
const flush = () => new Promise(resolve => setImmediate(resolve))

function gallery(extraArchive = false, tagGroups = [], customEntries = null, hostname = 'localhost', options = {}) {
    let resolveClassifications, rejectClassifications
    const classifications = new Promise((resolve, reject) => {
        resolveClassifications = resolve
        rejectClassifications = reject
    })
    let html = '', thumbnails = []
    const thumbnailPattern = /<a href="[^"]*" class="thumbnail" style="[^"]*"[^>]*>/g
    const node = {
        clientWidth: options.clientWidth || 472, style: {},
        querySelectorAll: selector => selector === '.thumbnail' ? thumbnails : tagGroups,
        get innerHTML() {
            let i = 0
            return html.replace(thumbnailPattern, anchor => {
                const style = Object.entries(thumbnails[i++].style).map(([key, value]) =>
                    `${key.replace(/[A-Z]/g, char => '-' + char.toLowerCase())}: ${value};`).join(' ')
                return anchor.replace(/style="[^"]*"/, `style="${style}"`)
            })
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
        },
    }
    const controls = Object.fromEntries(['filter-reactions', 'filter-template', 'filter-search', 'filter-status'].map(id =>
        [id, {
            value: id === 'filter-reactions' ? '0' : '', innerHTML: '',
            get options() { return [...this.innerHTML.matchAll(/value="([^"]*)"/g)].map(match => ({ value: match[1] })) },
            insertAdjacentHTML(position, html) { this.innerHTML += html },
            focus() { this.focused = true },
            addEventListener(event, handler) { this.handler = handler },
        }]))
    const entries = customEntries || ['2024-01-01_a.jpg', '2024-01-02_b.jpg'].map(name => ({
        name, w: 500, h: 400, x: 0, y: 0,
    }))
    const listeners = {}
    const lightbox = { options: {}, loadAndOpen(index) { this.openedIndex = index } }
    const requests = []
    const imageRequests = []
    let dialog
    const context = {
        document: {
            body: { appendChild(node) { dialog = node } },
            createElement() {
                const pre = { textContent: '' }
                return {
                    setAttribute() {}, addEventListener() {},
                    querySelector: () => pre,
                    showModal() { this.open = true },
                }
            },
            documentElement: { scrollTop: 0 },
            getElementById: id => id === 'image-debug-dialog' ? dialog : controls[id] || node,
        },
        location: { protocol: 'http:', host: hostname, hostname },
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
        setTimeout, clearTimeout,
        requestAnimationFrame: options.requestAnimationFrame || (callback => callback()),
        fetchJson: async path => {
            requests.push(path)
            if (path === 'images/dir_index.json') return options.dirIndex || (extraArchive
                ? { '2024/01': 2, '2024/02': 2, '2024/03': 2 } : { '2024/01': entries.length })
            if (path === 'images/telegram_metadata.json') return options.telegram || { '2024-01-02_b.jpg': [42, 7, 100, 2] }
            if (path === 'images/classification_index.json') {
                const index = await classifications
                return Object.fromEntries(Object.entries(index).map(([key, { text, description, ...entry }]) => [key, entry]))
            }
            if (path === 'images/classification_text_index.json') {
                if (options.textIndex) return options.textIndex
                const index = await classifications
                return Object.fromEntries(Object.entries(index).map(([key, { text, description }]) => [key, { text, description }]))
            }
            if (extraArchive && !path.includes('/2024/01/')) return entries.map(item => ({
                ...item, name: item.name.replace('2024-01', path.includes('/2024/02/') ? '2024-02' : '2024-03'),
            }))
            return entries
        },
        innerWidth: 400,
        innerHeight: options.innerHeight || 4 * 364,
        addEventListener: (event, handler) => {
            (listeners[event] ||= []).push(handler)
        },
        lightbox,
    }
    node.getBoundingClientRect = () => ({ top: (options.galleryTop || 0) - context.document.documentElement.scrollTop })
    context.window = context
    vm.runInNewContext(appSource, context)
    return { node, controls, resolveClassifications, rejectClassifications, listeners, lightbox, requests, imageRequests, context,
        get dialog() { return dialog } }
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
    assert.ok(!ui.requests.includes('images/telegram_metadata.json'))
    assert.equal(frames.length, 0)
    sprite.onload()
    await flush()
    frames.shift()()
    await flush()
    assert.ok(!ui.requests.includes('images/telegram_metadata.json'))
    frames.shift()()
    await flush()
    assert.ok(ui.requests.includes('images/telegram_metadata.json'))
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
    assert.ok(!ui.requests.includes('images/classification_text_index.json'))
    releaseTelegram({ '2024-01-02_b.jpg': [42, 7, 100, 2] })
    await flush()
    assert.equal(ui.requests.at(-1), 'images/classification_text_index.json')
    await applyFilters(ui, { 'filter-search': 'Sesamstraße' })
    assert.match(ui.node.innerHTML, /2024-01-02_b.jpg/)
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

test('debug is only rendered on loopback hosts', async () => {
    for (const hostname of ['localhost', '127.0.0.1', '[::1]', 'example.com', 'localhost.example.com']) {
        const ui = gallery(false, [], null, hostname)
        await flush()
        assert.equal(ui.node.innerHTML.includes('class="thumbnail-debug"'),
            ['localhost', '127.0.0.1', '[::1]'].includes(hostname))
        ui.resolveClassifications({})
        await flush()
    }
})

test('debug modal preserves all metadata safely and follows filtered indexes', async () => {
    const ui = gallery()
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
    assert.equal(ui.node.style.height, '364px')

    ui.resolveClassifications({ [imageId]: result, '2023/12/not-visible.jpg': result })
    await flush()
    assert.match(ui.node.innerHTML, /aria-label="Vorlage und Schlagwörter zum Bild"><button type="button" class="classification-tag meme-template">Example &quot;template&quot;<\/button>/)
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
    const clippedRow = tag({ bottom: 60, left: 0, right: 80 })
    const tooWide = tag({ bottom: 18, left: 0, right: 160 })
    const ui = gallery(false, [{
        getBoundingClientRect: () => ({ bottom: 54, left: 0, right: 150 }),
        children: [fitting, clippedRow, tooWide],
    }])
    await flush()
    assert.equal(fitting.style.visibility, 'visible')
    assert.equal(fitting.tabIndex, 0)
    for (const tag of [clippedRow, tooWide]) {
        assert.equal(tag.style.visibility, 'hidden')
        assert.equal(tag.disabled, true)
        assert.equal(tag.tabIndex, -1)
    }
    ui.resolveClassifications({})
    await flush()
})

test('clicking a tag fills the search and filters without opening the lightbox', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    ui.listeners.click[0]({
        target: { textContent: 'Sesamstraße', classList: { contains: name => name === 'classification-tag' } },
        preventDefault() {},
    })
    assert.equal(ui.controls['filter-search'].value, 'Sesamstraße')
    assert.equal(ui.controls['filter-search'].focused, true)
    await new Promise(resolve => setTimeout(resolve, 180))
    await flush()
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.equal(ui.lightbox.openedIndex, undefined)
})

test('clicking a template selects it, even when absent from the dropdown, without changing search', async () => {
    const ui = gallery()
    await flush()
    ui.resolveClassifications({ [imageId]: result })
    await flush()
    assert.doesNotMatch(ui.controls['filter-template'].innerHTML, /Example/)
    ui.controls['filter-search'].value = 'OCR'
    const click = () => ui.listeners.click[0]({
        target: {
            textContent: result.template,
            classList: { contains: name => ['classification-tag', 'meme-template'].includes(name) },
        },
        preventDefault() {},
    })
    click()
    assert.equal(ui.controls['filter-template'].value, result.template)
    assert.equal(ui.controls['filter-template'].focused, true)
    assert.match(ui.controls['filter-template'].innerHTML, /Example &quot;template&quot; \(1\)/)
    assert.equal(ui.controls['filter-search'].value, 'OCR')
    await new Promise(resolve => setTimeout(resolve, 180))
    await flush()
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.equal(ui.lightbox.openedIndex, undefined)
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
    assert.doesNotMatch(ui.node.innerHTML, /class="classification-tag"/)
    assert.doesNotMatch(ui.node.innerHTML, /Nicht klassifiziert/)
})

test('tags use catalog-wide image counts, hide singletons and sort most frequent first', async () => {
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
    assert.deepEqual(tagLabels(), ['Common', 'Alphabetical', 'Rare'])
    assert.deepEqual(visible.tags, ['Singleton', 'Rare', 'Common', 'Alphabetical', 'Duplicate only', 'duplicate ONLY'])
    // Filtering must not change frequencies or remove singleton tags from search.
    await applyFilters(ui, { 'filter-search': 'singleton' })
    assert.equal(ui.controls['filter-status'].textContent, '1 passende Bilder')
    assert.deepEqual(tagLabels(), ['Common', 'Alphabetical', 'Rare'])
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
    ui.controls['filter-search'].handler()
    await new Promise(resolve => setTimeout(resolve, 180))
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
    assert.equal(ui.node.style.height, '364px')
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
    assert.equal(ui.node.style.height, `${21 * 364}px`)
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
    ui.listeners[event][0]({ constructor: { name: 'Event' } })
    await new Promise(resolve => setTimeout(resolve, 180))
    await flush()
}

test('renders viewport rows plus one-row overscan and refreshes within a month', async () => {
    const ui = gallery(false, [], manyEntries(200))
    await flush()
    assert.equal(thumbnailCount(ui), 10)
    assert.deepEqual([...renderedSprites(ui)], ['images/2024/01/thumbnails-09.webp'])
    ui.resolveClassifications({})
    await flush()
    ui.context.document.documentElement.scrollTop = 14 * 364
    await updateViewport(ui)
    assert.equal(thumbnailCount(ui), 12)
    assert.doesNotMatch(ui.node.innerHTML, /-data-gallery-idx="0"/)
    assert.match(ui.node.innerHTML, /-data-gallery-idx="26"/)
    assert.deepEqual([...renderedSprites(ui)], ['images/2024/01/thumbnails-08.webp'])
    ui.context.document.documentElement.scrollTop = 20 * 364
    await updateViewport(ui)
    assert.deepEqual([...renderedSprites(ui)].sort(), ['images/2024/01/thumbnails-07.webp', 'images/2024/01/thumbnails-08.webp'])
})

test('twenty visible images need at most three sprites, including a partial newest sheet', async () => {
    const ui = gallery(false, [], manyEntries(201), 'localhost', { clientWidth: 5 * 236 })
    await flush()
    assert.equal(thumbnailCount(ui), 25)
    assert.equal(renderedSprites(ui).size, 3)
    ui.resolveClassifications({})
    await flush()
    assert.equal(renderedSprites(ui).size, 3)
    ui.context.document.documentElement.scrollTop = 10 * 364
    await updateViewport(ui)
    assert.equal(thumbnailCount(ui), 30)
    assert.ok(renderedSprites(ui).size <= 3)
})

test('window accounts for the gallery offset and changes when only viewport height changes', async () => {
    const ui = gallery(false, [], manyEntries(200), 'localhost', {
        clientWidth: 5 * 236, galleryTop: 2 * 364,
    })
    await flush()
    assert.equal(thumbnailCount(ui), 15)
    ui.context.innerHeight = 6 * 364
    await updateViewport(ui, 'resize')
    assert.equal(thumbnailCount(ui), 25)
    ui.resolveClassifications({})
    await flush()
})

test('filtered results load originals in the same viewport-sized window', async () => {
    const entries = manyEntries(201)
    const ui = gallery(false, [], entries, 'localhost', { clientWidth: 5 * 236 })
    ui.resolveClassifications(Object.fromEntries(entries.map(entry => [
        `2024/01/${entry.name}`, { tags: ['matching'], template: null },
    ])))
    await flush()
    await applyFilters(ui, { 'filter-search': 'matching' })
    const renderedOriginals = () => [...ui.node.innerHTML.matchAll(/background-image: url\('https:\/\/archive.example\/images\/[^']+'/g)]
    assert.equal(thumbnailCount(ui), 25)
    assert.equal(renderedOriginals().length, 25)
    assert.equal(renderedSprites(ui).size, 0)
    ui.context.document.documentElement.scrollTop = 10 * 364
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
        assert.match(ui.node.innerHTML, /background-position: center; background-size: contain; background-color: black;/)
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
    assert.match(ui.node.innerHTML, /background-size: contain; background-color: black;/)
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
    assert.equal(tile.style.backgroundColor, 'black')
    assert.equal(renderedSprites(ui).size, 1, 'other tile still shows its preview')
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
    ui.context.document.documentElement.scrollTop = 20 * 364
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

test('changing filters clears queued upgrades even before the debounced render', async () => {
    const ui = gallery(false, [], manyEntries(200))
    await flush()
    const originals = [...originalRequests(ui)]
    assert.equal(originals.length, 4)
    ui.controls['filter-search'].value = 'no matches'
    ui.controls['filter-search'].handler()
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
        clientWidth: 5 * 236, imageLoad() {},
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

test('220 px tiles and gallery geometry support five columns at full width', () => {
    const style = readFileSync(`${__dirname}/../assets/style.css`, 'utf8')
    assert.match(style, /\.thumbnail \{[^}]*width: 220px;\s*height: 220px;/)
    assert.match(style, /\.gallery-item \{[^}]*width: 236px;\s*height: 364px;/)
    const containerWidth = Number(style.match(/\.container \{[^}]*max-width: (\d+)px;/)[1])
    assert.ok(containerWidth - 80 >= 5 * 236, 'five cards must fit inside the gallery margins')
})
