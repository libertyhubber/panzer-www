(function(){
"strict";

// Keep sprite geometry in sync with scripts/generate_thumbnails.py and style.css.
const THUMBNAIL_SIZE = 220
const IMAGES_PER_SHEET = 20
const SHEET_COLUMNS = 5
const THUMBNAIL_PADDING = 2
const THUMBNAIL_SHEET_WIDTH = SHEET_COLUMNS * THUMBNAIL_SIZE + (SHEET_COLUMNS - 1) * THUMBNAIL_PADDING
const COMPACT_HORIZONTAL_GAP = 7
const COMPACT_OUTER_GUTTER = 4
const MIN_TWO_COLUMN_WIDTH = 320
const THUMBNAIL_GAP = 8
const MAX_HORIZONTAL_GAP = 20
const THUMBNAIL_WIDTH = THUMBNAIL_SIZE + THUMBNAIL_GAP
const GALLERY_CARD_HEIGHT = 290
const VERTICAL_GAP = 15
const DEBUG_ENABLED = new URL(location.href).searchParams.get('debug') === '1'
const ORIGINAL_LOAD_LIMIT = 4
const GALLERY_IMAGES = new Map()
let upgradeCandidates = []
let activeOriginalLoads = 0
let galleryScrolling = false
let galleryFramePending = false
let galleryRenderVersion = 0
let fittedClassifications = new Map()
const metadataMonths = new Map()
const metadataFailures = new Set()
const metadataEnabled = new Set()
const metadataKinds = {
    telegram: ['telegram_metadata.json', 'telegramMetadata', 'telegramStatus'],
    classification: ['classification_index.json', 'classificationIndex', 'classificationStatus'],
    text: ['classification_text_index.json', 'classificationTextIndex', 'classificationTextStatus'],
}

const GALLERY_STATE = {
    'webpSupported': false,
    'dirIndex': null, // {dirName: numEntries, ....}
    'dirNames': null, // [dirName, ....]
    'telegramMetadata': {}, // {filename: [messageId, reactionCount, views, comments], ...}
    'telegramStatus': 'loading',
    'classificationIndex': {}, // {"YYYY/MM/filename": {tags, tags_de?, tags_en?, tag_format_version?, template}}
    'classificationStatus': 'loading',
    'classificationTextIndex': {}, // {"YYYY/MM/filename": {text, description}}
    'classificationTextStatus': 'loading',
    'tagCounts': new Map(), // Normalized tag -> number of classified images across the catalog.
    'templateCounts': new Map(), // Template -> number of classified images across the catalog.
    'totalEntries': -1,
    'debounceTimeout': null,
    'searchDebounceTimeout': null,
    'lastRenderState': null,
    'dataSource': null,
    'allItemsPromise': null,
    'allItemsComplete': false, // All archive requests settled, including failures.
    'allItemsError': false,
    'filteredItems': null,
    'filters': { minReactions: 0, template: '', search: '' },
    'filterVersion': 0,
    'navigationNotice': '',
    'workerError': false,
}

let searchWorker = null
let workerRequestId = 0
const workerRequests = new Map()
let archiveResolve = null
let filterQuery = null
const pageFetchJson = window.fetchJson

function workerFailed(error) {
    console.warn('Gallery search worker unavailable', error)
    searchWorker?.terminate()
    searchWorker = null
    GALLERY_STATE.workerError = true
    GALLERY_STATE.allItemsError = true
    GALLERY_STATE.allItemsComplete = true
    GALLERY_STATE.filteredItems = []
    for (const request of workerRequests.values()) {
        pageFetchJson(request.path).then(request.resolve, request.reject)
    }
    workerRequests.clear()
    archiveResolve?.()
    archiveResolve = null
    filterQuery?.resolve()
    GALLERY_STATE.lastRenderState = null
    updateGallery()
}

function getSearchWorker() {
    if (!searchWorker && !GALLERY_STATE.workerError) {
        try {
            const worker = new Worker(`/assets/search-worker.js?v=2&cb=${CB}`)
            searchWorker = worker
            worker.onmessage = event => { if (searchWorker === worker) workerMessage(event) }
            worker.onerror = event => {
                event.preventDefault?.()
                if (searchWorker === worker) workerFailed(new Error(event.message || 'Worker failed'))
            }
            worker.onmessageerror = () => {
                if (searchWorker === worker) workerFailed(new Error('Invalid worker message'))
            }
            worker.postMessage({ type: 'init', baseURL: location.href, cacheBust: CB })
        } catch (error) {
            workerFailed(error)
        }
    }
    return searchWorker
}

// Browsing and archive searches share the worker's HTTP/JSON cache. If workers
// are unavailable, normal browsing still works; do not silently scan on the UI thread.
async function fetchJson(path) {
    const worker = getSearchWorker()
    if (!worker) return pageFetchJson(path)
    const id = ++workerRequestId
    return new Promise((resolve, reject) => {
        workerRequests.set(id, { resolve, reject, path })
        worker.postMessage({ type: 'fetch', id, path })
    })
}

function applyMonthMetadata(dirName, kind, data) {
    const key = `${kind}:${dirName}`
    const [, field, status] = metadataKinds[kind]
    Object.assign(GALLERY_STATE[field], data)
    metadataFailures.delete(key)
    GALLERY_STATE[status] = [...metadataFailures].some(key => key.startsWith(`${kind}:`))
        ? 'unavailable' : 'ready'
    if (kind === 'telegram') {
        // Only this month's entries need enrichment, not the entire archive.
        for (const filename of Object.keys(data)) {
            const item = monthItems.get(`${dirName}/${filename}`)
            if (item) Object.assign(item, telegramFields(filename))
        }
    }
}

function workerMessage({ data }) {
    if (data.type === 'fetched') {
        const request = workerRequests.get(data.id)
        if (!request) return
        workerRequests.delete(data.id)
        if (data.error) request.reject(new Error(data.error))
        else request.resolve(data.value)
    } else if (data.type === 'month') {
        storeMonthItems(data.dirName, data.start, data.entries)
    } else if (data.type === 'metadata') {
        if (data.error) {
            metadataFailures.add(`${data.kind}:${data.dirName}`)
            GALLERY_STATE[metadataKinds[data.kind][2]] = 'unavailable'
        } else {
            applyMonthMetadata(data.dirName, data.kind, data.data)
            metadataMonths.set(`${data.kind}:${data.dirName}`, Promise.resolve(true))
        }
    } else if (data.type === 'loaded') {
        GALLERY_STATE.allItemsComplete = true
        GALLERY_STATE.allItemsError = data.error
        archiveResolve?.()
        archiveResolve = null
    } else if (data.type === 'results') {
        // Results for old filters must never replace a newer query or history entry.
        if (!filtersActive() || data.id !== GALLERY_STATE.filterVersion || data.id !== filterQuery?.version) return
        // Resolve IDs to existing items; do not clone the entire matching catalog.
        GALLERY_STATE.filteredItems = data.indices.map(index => GALLERY_STATE.dataSource[index])
        GALLERY_STATE.allItemsComplete = data.complete
        GALLERY_STATE.allItemsError = data.error
        for (const [kind, status] of Object.entries(data.statuses)) {
            GALLERY_STATE[metadataKinds[kind][2]] = status
        }
        GALLERY_STATE.lastRenderState = null
        filterQuery.resolve()
        updateGallery()
    } else if (data.type === 'fatal') {
        workerFailed(new Error(data.error))
    }
}

function cancelFilterQuery() {
    filterQuery?.resolve()
    if (!filtersActive()) searchWorker?.postMessage({ type: 'cancel' })
}

function requestFilter() {
    const version = GALLERY_STATE.filterVersion
    if (filterQuery?.version === version) return filterQuery.promise
    filterQuery?.resolve()
    let resolve
    const promise = new Promise(done => { resolve = done })
    filterQuery = { version, promise, resolve }
    const worker = getSearchWorker()
    if (worker) worker.postMessage({ type: 'query', id: version, filters: GALLERY_STATE.filters })
    else resolve()
    return promise
}

const monthItems = new Map()

// Only durable browsing state belongs in the URL, not transient overlays or indexes.
// Image filenames remain stable when newer images are added to the archive.
let navigationVersion = 0
let restoringNavigation = false
let selectedImageId = ''
let appliedSearch = ''
let resolveMetadataReady, resolveGalleryReady
const metadataReady = new Promise(resolve => { resolveMetadataReady = resolve })
const galleryReady = new Promise(resolve => { resolveGalleryReady = resolve })

function nonnegativeNumber(value) {
    const number = Number(value)
    return Number.isFinite(number) ? Math.max(0, number) : 0
}

function readNavigation() {
    const params = new URL(location.href).searchParams
    return {
        minReactions: nonnegativeNumber(params.get('reactions')),
        template: params.get('template') || '',
        search: params.get('q') || '',
        image: params.get('image') || '',
        // Scroll is local to this tab's history, never part of a shared URL.
        scroll: nonnegativeNumber(history.state?.galleryScroll),
    }
}

function writeNavigation(push = false) {
    if (restoringNavigation) return
    const url = new URL(location.href)
    const values = {
        reactions: GALLERY_STATE.filters.minReactions || '',
        template: GALLERY_STATE.filters.template,
        q: appliedSearch,
        image: selectedImageId,
    }
    for (const [key, value] of Object.entries(values)) {
        if (value === '') url.searchParams.delete(key)
        else url.searchParams.set(key, value)
    }
    // Preserve unrelated query parameters/fragments and other history state.
    const scroll = Math.round(document.documentElement.scrollTop)
    const state = { ...history.state, galleryScroll: scroll }
    if (url.href !== location.href) {
        history[push ? 'pushState' : 'replaceState'](state, '', url.href)
    } else if (history.state?.galleryScroll !== scroll) {
        history.replaceState(state, '', url.href)
    }
}

function setTemplateControl(value) {
    const control = document.getElementById('filter-template')
    if (value && !Array.from(control.options).some(option => option.value === value)) {
        control.insertAdjacentHTML('beforeend', templateOptionHTML(value,
            GALLERY_STATE.templateCounts.get(value) || 0))
    }
    control.value = value
}

function cancelLightbox() {
    // Cancel a pending module import too, before it can open an obsolete image.
    window.lightbox.shouldOpen = false
    const pswp = window.lightbox.pswp
    if (!pswp) return
    return new Promise(resolve => {
        pswp.on('destroy', resolve)
        if (pswp.opener.isOpen || pswp.isDestroying) pswp.destroy()
        else pswp.on('openingAnimationEnd', () => pswp.destroy())
    })
}

async function restoreNavigation(navigation) {
    const version = ++navigationVersion
    restoringNavigation = true
    clearTimeout(GALLERY_STATE.searchDebounceTimeout)
    clearTimeout(GALLERY_STATE.debounceTimeout)
    galleryScrolling = false
    galleryRenderVersion += 1
    upgradeCandidates = []
    fittedClassifications.clear()
    closeTagOverlay()
    const lightboxClosed = cancelLightbox()
    selectedImageId = navigation.image
    GALLERY_STATE.navigationNotice = ''
    appliedSearch = navigation.search
    document.getElementById('filter-reactions').value = navigation.minReactions
    document.getElementById('filter-search').value = navigation.search
    updateSearchControl()
    GALLERY_STATE.filters = {
        minReactions: navigation.minReactions,
        template: navigation.template,
        search: normalizeSearch(navigation.search),
    }
    setTemplateControl(navigation.template)
    cancelFilterQuery()
    GALLERY_STATE.filterVersion += 1
    GALLERY_STATE.filteredItems = null
    GALLERY_STATE.lastRenderState = null
    try {
        // Plain image links need only their month; filtered links require the final
        // metadata/indexes so partial results cannot open the wrong slide.
        await Promise.all([filtersActive() ? metadataReady : galleryReady, lightboxClosed])
        if (version !== navigationVersion) return
        if (filtersActive()) await loadAllItems()
        else if (navigation.image) {
            const dirName = navigation.image.split('/').slice(0, 2).join('/')
            let start = 0
            for (const dir of [...GALLERY_STATE.dirNames].reverse()) {
                if (dir === dirName) {
                    try {
                        await loadMonthItems(dir, start)
                    } catch (error) {
                        console.warn('Could not load linked image month', error)
                    }
                    break
                }
                start += GALLERY_STATE.dirIndex[dir]
            }
        }
        if (version !== navigationVersion) return
        // Refresh the gallery height before restoring a scroll position: the old
        // filter may have had too few rows for the browser to scroll this far.
        await updateGallery()
        if (version !== navigationVersion) return
        window.scrollTo(0, navigation.scroll)
        await updateGallery()
        if (version !== navigationVersion) return
        if (navigation.image) {
            const items = filtersActive() ? GALLERY_STATE.filteredItems : GALLERY_STATE.dataSource
            const index = items.findIndex(item => item?.imageId === navigation.image)
            if (index >= 0) {
                window.lightbox.options.dataSource = items
                window.lightbox.loadAndOpen(index)
            } else {
                GALLERY_STATE.navigationNotice = ' (Verlinktes Bild nicht verfügbar oder durch Filter ausgeschlossen.)'
                document.getElementById('filter-status').textContent += GALLERY_STATE.navigationNotice
            }
        }
    } catch (error) {
        console.warn('Could not restore gallery navigation', error)
    } finally {
        if (version === navigationVersion) restoringNavigation = false
    }
}

async function lightboxChangeHandler() {
    const pswp = window.lightbox.pswp
    if (!pswp || pswp.isDestroying) return
    const version = navigationVersion
    const index = pswp.currIndex
    const currentItem = pswp.options.dataSource[index]
    if (currentItem) {
        selectedImageId = currentItem.imageId
        writeNavigation()
    }
    if (!filtersActive()) {
        try {
            await preloadLightboxItems(index)
        } catch (error) {
            console.warn('Could not preload neighboring images', error)
        }
    }
    if (version !== navigationVersion || pswp !== window.lightbox.pswp || pswp.isDestroying || index !== pswp.currIndex) return
    const item = pswp.options.dataSource[index]
    if (item) {
        selectedImageId = item.imageId
        writeNavigation()
    }
}

function lightboxCloseHandler() {
    if (restoringNavigation) return
    navigationVersion += 1
    selectedImageId = ''
    // Closing is a navigation action: Back can reopen the image, Forward closes it.
    writeNavigation(true)
}

async function loadMonthItems(dirName, dirStartIndex, enrich = true) {
    // Local indexes and sprites are generated together; only originals use IMG_HOSTS.
    const entryIndex = await fetchJson(`images/${dirName}/entry_index.json`)
    const items = storeMonthItems(dirName, dirStartIndex, entryIndex)
    // Browsing never waits for optional metadata. Enable each kind only after
    // its initial paint gate, then enrich new months as they are encountered.
    if (enrich) for (const kind of metadataEnabled) loadMonthMetadata(dirName, kind)
    return items
}

function storeMonthItems(dirName, dirStartIndex, entryIndex) {
    const dataSourceItems = []
    const fallbackHost = location.protocol + "//" + location.host
    const host = IMG_HOSTS[dirName.split("/")[0]] || fallbackHost

    for (let j = entryIndex.length - 1; j >= 0; j--) {
        const entry = entryIndex[j]
        const dateMatch = entry.name.match(/^(\d{4})-(\d{2})-(\d{2})/)
        const sheetIndex = Math.floor(j / IMAGES_PER_SHEET)
        const localIndex = j % IMAGES_PER_SHEET
        const stride = THUMBNAIL_SIZE + THUMBNAIL_PADDING
        const thumbSrc = `images/${dirName}/thumbnails-${String(sheetIndex).padStart(2, '0')}.webp?cb=${CB}`
        dataSourceItems.push({
            ...(DEBUG_ENABLED ? { entryMetadata: entry } : {}),
            imageId: `${dirName}/${entry.name}`,
            src: `${host}/images/${dirName}/${entry.name}`,
            width: entry.w,
            height: entry.h,
            bg: typeof entry.bg === 'string' && /^[0-9a-f]{3}$/i.test(entry.bg) ? entry.bg : '000',
            bgOffsetX: (localIndex % SHEET_COLUMNS) * stride,
            bgOffsetY: Math.floor(localIndex / SHEET_COLUMNS) * stride,
            thumbSrc: thumbSrc,
            date: dateMatch ? `${dateMatch[1]}-${dateMatch[2]}-${dateMatch[3]}` : null,
            ...telegramFields(entry.name),
            galleryIndex: dirStartIndex + dataSourceItems.length,
        })
    }
    for (const item of dataSourceItems) {
        GALLERY_STATE.dataSource[item.galleryIndex] = item
        monthItems.set(item.imageId, item)
    }
    return dataSourceItems
}

async function updateDataSources(itemIndex) {
    // scan through directories
    var dirStartIndex = 0
    for (var dirCursor = GALLERY_STATE.dirNames.length - 1; dirCursor >= 0; dirCursor--) {
        var dirEntryCount = GALLERY_STATE.dirIndex[GALLERY_STATE.dirNames[dirCursor]]
        if (itemIndex >= dirStartIndex + dirEntryCount) {
            dirStartIndex += dirEntryCount
        } else {
            break
        }
    }

    const dirNames = []
    const dirStarts = []
    let nextStart = dirStartIndex

    for (var i = dirCursor; i >= Math.max(0, dirCursor - 1); i--) {
        var dirName = GALLERY_STATE.dirNames[i]
        dirNames.push(dirName)

        dirStarts.push(nextStart)
        nextStart += GALLERY_STATE.dirIndex[dirName]
    }

    const batches = await Promise.all(dirNames.map((dirName, i) =>
        loadMonthItems(dirName, dirStarts[i])))
    const dataSourceItems = batches.flat()

    return {
       dirCursor: dirCursor,
       dirStartIndex: dirStartIndex,
       dataSourceItems: dataSourceItems,
    }
}


function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char])
}

function templateLabel(name) {
    const maxLength = 24
    const chars = [...name]
    if (chars.length <= maxLength) return name
    let prefix = chars.slice(0, maxLength - 1).join('')
    // Prefer whole words, but still shorten a single unusually long word.
    if (!/\s/.test(chars[maxLength - 1]) && /\s/.test(prefix)) {
        prefix = prefix.replace(/\s+\S*$/, '')
    }
    return prefix.trimEnd() + '…'
}

function templateOptionHTML(name, count) {
    return `<option title="${escapeHtml(name)}" value="${escapeHtml(name)}">${escapeHtml(templateLabel(name))} (${count})</option>`
}

function tagKey(tag) {
    return tag.trim().toLowerCase()
}

// Compatibility fallback for legacy tags without language metadata. Recognize English vocabulary;
// shared words (meme, comic, satire, humor, etc.) and unrecognized names stay inline.
// This is a display hint, never a change to the classification/search data.
const ENGLISH_TAG_WORDS = new Set((
    'political reaction caption captioned speech bubbles bubble image images ' +
    'german english dark black white criticism state government taxes theft ' +
    'communism socialism capitalism libertarianism statism anarchism libertarians ' +
    'statists socialists politicians democracy freedom cryptocurrency comparison ' +
    'overlay over with without two three four part close up ' +
    'sunglasses glasses skeptical look expression double standards tax office ' +
    'woman women bearded crowd hammer sickle conspiracy theory theories ' +
    'media split pun climate change classroom serious flag soldier police ' +
    'military money economy election voting censorship surveillance ' +
    'health healthcare education school teacher children child cat dog ' +
    'funny angry happy sad surprised smiling laughing crying pointing sitting ' +
    'standing wearing suit the of and'
).split(' '))

function isClearlyEnglishTag(tag) {
    // Keep mixed-language tags with explicit German characters in the preview.
    if (/[äöüß]/i.test(tag)) return false
    return tagKey(tag).split(/[^a-z]+/).some(word => ENGLISH_TAG_WORDS.has(word))
}

function classificationTags(classification) {
    if (!classification) return []
    if (!(classification.tag_format_version >= 2)) return classification.tags || []
    const seen = new Set()
    return ['tags', 'tags_de', 'tags_en'].flatMap(field => classification[field] || [])
        .filter(tag => {
            const key = tagKey(tag)
            if (!key || seen.has(key)) return false
            seen.add(key)
            return true
        })
}

function classificationHTML(classification, imageId) {
    if (!classification) {
        const message = GALLERY_STATE.classificationStatus === 'loading'
            ? 'Klassifizierung wird geladen…'
            : GALLERY_STATE.classificationStatus === 'unavailable'
                ? 'Klassifizierung nicht verfügbar' : 'Nicht klassifiziert'
        return `<div class="thumbnail-classification classification-unavailable">${message}</div>`
    }
    const template = (GALLERY_STATE.templateCounts.get(classification.template) || 0) > 1
        ? `<button type="button" data-template="${escapeHtml(classification.template)}" title="${escapeHtml(classification.template)}" aria-label="${escapeHtml(classification.template)}" class="classification-tag meme-template">${escapeHtml(templateLabel(classification.template))}</button>`
        : ''
    // Explicit language lists take precedence, including an explicitly empty list.
    // Neutral tags (shared/unresolved) are inline too in format 2. Legacy format 1
    // still has combined tags, so only its explicit German membership is inline.
    const germanTags = Array.isArray(classification.tags_de)
        ? new Set([...classification.tags_de,
            ...(classification.tag_format_version >= 2 ? classification.tags : [])].map(tagKey)) : null
    const overlayOnly = tag => germanTags !== null
        ? !germanTags.has(tagKey(tag)) : isClearlyEnglishTag(tag)
    // The catalog omits singleton tags to keep the global summary small.
    const frequency = tag => GALLERY_STATE.tagCounts.get(tagKey(tag)) || 1
    const allTags = classificationTags(classification)
    const sortedTags = [...allTags]
        .sort((a, b) => frequency(b) - frequency(a) || a.localeCompare(b, 'de'))
    const tags = sortedTags
        .map(tag => `<button type="button" class="classification-tag"${overlayOnly(tag) ? ' data-overlay-only="true"' : ''}>${escapeHtml(tag)}</button>`)
        .join('')
    return `<div class="thumbnail-classification" data-image-id="${escapeHtml(imageId)}">` +
        (template || allTags.length ?
            `<div class="classification-tags" role="group" aria-label="Vorlage und Schlagwörter zum Bild">${template}${tags}` +
            `<button type="button" class="classification-more" data-image-id="${escapeHtml(imageId)}" aria-label="Weitere Schlagwörter anzeigen" title="Weitere Schlagwörter anzeigen" aria-haspopup="dialog" aria-expanded="false" hidden>+0</button></div>` : '') +
        '</div>'
}

function telegramFields(filename) {
    const telegram = GALLERY_STATE.telegramMetadata[filename]
    return {
        telegramMessageId: telegram ? telegram[0] : null,
        reactions: telegram ? telegram[1] : null,
        views: telegram ? telegram[2] ?? null : null,
        comments: telegram ? telegram[3] ?? null : null,
    }
}

function supportsWebp() {
    // Test lossy WebP decoding, rather than browser versions or MIME support.
    return new Promise(resolve => {
        const image = new Image()
        image.onload = () => resolve(image.width === 1 && image.height === 1)
        image.onerror = () => resolve(false)
        image.src = 'data:image/webp;base64,UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAsBIJaQAA3AA/vjMoAA='
    })
}

function galleryImage(src, priority = 'auto') {
    if (!GALLERY_IMAGES.has(src)) {
        const state = { status: 'loading', painted: false }
        GALLERY_IMAGES.set(src, state)
        state.promise = new Promise(resolve => {
            const image = new Image()
            image.decoding = 'async'
            image.fetchPriority = priority
            const finish = loaded => {
                state.status = loaded ? 'loaded' : 'failed'
                resolve(loaded)
            }
            image.onerror = () => finish(false)
            image.onload = async () => {
                try {
                    // Keep the sprite visible until the original is also decoded.
                    if (image.decode) await image.decode()
                    finish(true)
                } catch (error) {
                    finish(false)
                }
            }
            image.src = src
        })
    }
    return GALLERY_IMAGES.get(src)
}

function afterImagePaint() {
    return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
}

function galleryImagePaint(src) {
    const state = galleryImage(src)
    if (!state.paintPromise) {
        state.paintPromise = state.promise.then(async loaded => {
            await afterImagePaint()
            state.painted = loaded
            return loaded
        })
    }
    return state.paintPromise
}

function usesOriginal(item, filtered) {
    return filtered || !GALLERY_STATE.webpSupported ||
        GALLERY_IMAGES.get(item.src)?.status === 'loaded' ||
        GALLERY_IMAGES.get(item.thumbSrc)?.status === 'failed'
}

// CSSOM normalizes URLs, colors and positions. Cache our desired paint rather
// than comparing serialized styles, which could reassign a loading URL on scroll.
function paintGalleryThumbnail(thumbnail, src, position, size, bg) {
    const paint = JSON.stringify([src, position, size, bg])
    if (thumbnail.galleryPaint === paint) return
    if (thumbnail.galleryImageSrc !== src) {
        thumbnail.style.backgroundImage = `url('${src}')`
        thumbnail.galleryImageSrc = src
    }
    thumbnail.style.backgroundPosition = position
    thumbnail.style.backgroundSize = size
    thumbnail.style.backgroundColor = `#${bg}`
    thumbnail.galleryPaint = paint
}

function showOriginal(item) {
    if (galleryScrolling && GALLERY_IMAGES.get(item.src)?.status !== 'loaded') return
    galleryImage(item.src)
    // Only mutate currently rendered links; async loads may outlive a filter or scroll.
    for (const thumbnail of document.getElementById('gallery').querySelectorAll('.thumbnail')) {
        if (thumbnail.getAttribute('href') !== item.src) continue
        paintGalleryThumbnail(thumbnail, item.src, 'center', 'contain', item.bg)
    }
}

function loadOriginals() {
    if (galleryScrolling) return
    while (activeOriginalLoads < ORIGINAL_LOAD_LIMIT) {
        const item = upgradeCandidates.find(item =>
            !GALLERY_IMAGES.has(item.src) && GALLERY_IMAGES.get(item.thumbSrc)?.painted)
        if (!item) break
        activeOriginalLoads += 1
        galleryImage(item.src, 'low').promise.then(loaded => {
            activeOriginalLoads -= 1
            if (loaded) showOriginal(item)
            // A failed original leaves its sprite preview intact, without retry loops.
            loadOriginals()
        })
    }
}

function enhanceThumbnails(items, filtered) {
    if (galleryScrolling) return
    // Track direct CSS loads too, so cached originals remain visible during scroll.
    for (const item of items) {
        if (usesOriginal(item, filtered)) galleryImage(item.src)
    }
    // Replacing this list drops queued offscreen images; at most four old requests
    // can remain in flight. Loaded originals survive metadata renders and scrolling.
    upgradeCandidates = filtered || !GALLERY_STATE.webpSupported ? [] : items
    const sheets = new Set(upgradeCandidates.filter(item => !usesOriginal(item, filtered))
        .map(item => item.thumbSrc))
    for (const src of sheets) {
        const state = galleryImage(src)
        if (state.enhancementScheduled) continue
        state.enhancementScheduled = true
        galleryImagePaint(src).then(loaded => {
            if (!loaded) {
                // Missing/corrupt sheets are optional too, not broken gallery tiles.
                for (const item of upgradeCandidates) {
                    if (item.thumbSrc === src) showOriginal(item)
                }
            }
            loadOriginals()
        })
    }
    loadOriginals()
}

async function loadMonthMetadata(dirName, kind) {
    const key = `${kind}:${dirName}`
    if (!metadataMonths.has(key)) {
        const [filename, , status] = metadataKinds[kind]
        metadataMonths.set(key, (async () => {
            try {
                applyMonthMetadata(dirName, kind, await fetchJson(`images/${dirName}/${filename}`))
                return true
            } catch (error) {
                metadataFailures.add(key)
                GALLERY_STATE[status] = 'unavailable'
                console.warn(`Could not load ${kind} metadata for ${dirName}`, error)
                return false
            } finally {
                GALLERY_STATE.lastRenderState = null
                updateGallery()
            }
        })())
    }
    return metadataMonths.get(key)
}

async function enableMetadata(kind) {
    metadataEnabled.add(kind)
    const months = new Set(GALLERY_STATE.dataSource.filter(Boolean)
        .map(item => item.imageId.split('/').slice(0, 2).join('/')))
    await Promise.all([...months].map(month => loadMonthMetadata(month, kind)))
}

async function loadTelegramMetadata(firstThumbSrc) {
    // Share the first image's load/paint gate with lazy quality enhancement.
    if (firstThumbSrc) await galleryImagePaint(firstThumbSrc)
    else await afterImagePaint()
    await enableMetadata('telegram')
    GALLERY_STATE.lastRenderState = null
    await updateGallery()
}

async function loadClassifications() {
    const monthsReady = enableMetadata('classification')
    try {
        const catalog = await fetchJson('images/classification_catalog.json')
        GALLERY_STATE.tagCounts = new Map(Object.entries(catalog.tagCounts))
        GALLERY_STATE.templateCounts = new Map(Object.entries(catalog.templateCounts))
    } catch (error) {
        metadataFailures.add('classification:catalog')
        GALLERY_STATE.classificationStatus = 'unavailable'
        console.warn('Could not load classification catalog', error)
    }
    await monthsReady
    const select = document.getElementById('filter-template')
    const templateCounts = GALLERY_STATE.templateCounts
    const templates = [...templateCounts.entries()]
        .filter(([, count]) => count >= 5)
        .sort(([a], [b]) => a.localeCompare(b))
    select.innerHTML = '<option value="">Alle Vorlagen</option>' + templates.map(([name, count]) =>
        templateOptionHTML(name, count)).join('')
    setTemplateControl(GALLERY_STATE.filters.template)
    select.disabled = GALLERY_STATE.classificationStatus !== 'ready'
    GALLERY_STATE.lastRenderState = null
    await updateGallery()
}

async function loadClassificationText() {
    await enableMetadata('text')
    const search = document.getElementById('filter-search')
    search.placeholder = GALLERY_STATE.classificationTextStatus === 'ready'
        ? 'Bildtexte, Beschreibungen und Schlagwörter durchsuchen'
        : 'Schlagwörter durchsuchen (Volltextsuche nicht verfügbar)'
    GALLERY_STATE.lastRenderState = null
    await updateGallery()
}

function filtersActive() {
    const filters = GALLERY_STATE.filters
    return filters.minReactions > 0 || filters.template !== '' || filters.search !== ''
}

async function loadAllItems() {
    if (!GALLERY_STATE.allItemsPromise) {
        const worker = getSearchWorker()
        if (!worker) return
        GALLERY_STATE.allItemsError = false
        GALLERY_STATE.allItemsComplete = false
        GALLERY_STATE.allItemsPromise = new Promise(resolve => { archiveResolve = resolve })
        worker.postMessage({ type: 'load', dirIndex: GALLERY_STATE.dirIndex })
    }
    await GALLERY_STATE.allItemsPromise
}

function normalizeSearch(value) {
    return value.normalize('NFC').replace(/\s+/g, ' ').trim().toLowerCase()
        .replace(/[äöü]/g, char => ({ ä: 'a', ö: 'o', ü: 'u' })[char])
}

function updateSearchControl() {
    const hasValue = document.getElementById('filter-search').value !== ''
    document.getElementById('filter-search-clear').hidden = !hasValue
    document.getElementById('filter-search-icon').hidden = hasValue
}

function searchInputHandler() {
    updateSearchControl()
    clearTimeout(GALLERY_STATE.searchDebounceTimeout)
    GALLERY_STATE.searchDebounceTimeout = setTimeout(filterChangeHandler, 500)
}

function filterChangeHandler() {
    updateSearchControl()
    closeTagOverlay()
    // Other controls and tag clicks apply the current search immediately too.
    clearTimeout(GALLERY_STATE.searchDebounceTimeout)
    navigationVersion += 1
    restoringNavigation = false
    selectedImageId = ''
    GALLERY_STATE.navigationNotice = ''
    appliedSearch = document.getElementById('filter-search').value.trim()
    GALLERY_STATE.filters = {
        minReactions: nonnegativeNumber(document.getElementById('filter-reactions').value),
        template: document.getElementById('filter-template').value,
        search: normalizeSearch(document.getElementById('filter-search').value),
    }
    window.scrollTo(0, 0)
    writeNavigation(true)
    cancelFilterQuery()
    if (GALLERY_STATE.allItemsError) {
        GALLERY_STATE.allItemsPromise = null
        GALLERY_STATE.workerError = false
        filterQuery?.resolve()
        filterQuery = null
        for (const [key, promise] of metadataMonths) {
            promise.then(loaded => { if (!loaded) metadataMonths.delete(key) })
        }
    }
    GALLERY_STATE.filterVersion += 1
    galleryScrolling = false
    galleryRenderVersion += 1
    upgradeCandidates = []
    fittedClassifications.clear()
    document.getElementById('gallery').innerHTML = ''
    GALLERY_STATE.filteredItems = null
    GALLERY_STATE.lastRenderState = null
    clearTimeout(GALLERY_STATE.debounceTimeout)
    GALLERY_STATE.debounceTimeout = setTimeout(updateGallery, 150)
}

async function updateGallery() {
    if (!GALLERY_STATE.dirNames) {return}  // not yet initialized
    const renderVersion = ++galleryRenderVersion

    const galleryNode = document.getElementById("gallery")

    const galleryWidth = galleryNode.clientWidth
    const compact = galleryWidth < 2 * THUMBNAIL_WIDTH
    // Narrow galleries fit two whole-pixel thumbnails with a 7px gap and at
    // least 4px outer gutters. Fall back to one column below 320px.
    const tnColumns = compact ? (galleryWidth >= MIN_TWO_COLUMN_WIDTH ? 2 : 1)
        : Math.floor(galleryWidth / THUMBNAIL_WIDTH)
    const cardWidth = compact ? Math.max(1, Math.min(THUMBNAIL_SIZE, Math.floor(
        (galleryWidth - 2 * COMPACT_OUTER_GUTTER - (tnColumns - 1) * COMPACT_HORIZONTAL_GAP) / tnColumns
    ))) : THUMBNAIL_SIZE
    // Full-size cards keep the existing 8–20px gaps, favoring more columns.
    const horizontalGap = compact ? COMPACT_HORIZONTAL_GAP : Math.min(MAX_HORIZONTAL_GAP,
        Math.max(THUMBNAIL_GAP, galleryWidth / tnColumns - THUMBNAIL_SIZE))
    // Shrink only the image portion; keep metadata text and its space unscaled.
    const cardHeight = GALLERY_CARD_HEIGHT - THUMBNAIL_SIZE + cardWidth
    const rowHeight = cardHeight + VERTICAL_GAP
    const columnWidth = cardWidth + horizontalGap
    const marginLeft = (galleryWidth - tnColumns * cardWidth - (tnColumns - 1) * horizontalGap) / 2
    const spriteScale = cardWidth / THUMBNAIL_SIZE
    galleryNode.style.setProperty('--thumbnail-display-size', `${cardWidth}px`)
    galleryNode.style.setProperty('--gallery-card-height', `${cardHeight}px`)

    const version = GALLERY_STATE.filterVersion
    const filtered = filtersActive()
    const status = document.getElementById('filter-status')
    if (filtered && !GALLERY_STATE.filteredItems) {
        // Await only the first response; later archive matches arrive progressively.
        document.getElementById('gallery').setAttribute?.('aria-busy', 'true')
        status.textContent = 'lade Memes...'
        loadAllItems()
        await requestFilter()
        if (renderVersion !== galleryRenderVersion || version !== GALLERY_STATE.filterVersion) return
    }
    const totalEntries = filtered ? (GALLERY_STATE.filteredItems || []).length : GALLERY_STATE.totalEntries
    galleryNode.setAttribute?.('aria-busy', String(filtered && !GALLERY_STATE.allItemsComplete))
    const totalRows = Math.ceil(totalEntries / tnColumns)
    galleryNode.style.height = (totalRows * rowHeight) + "px"
    const { search, template, minReactions } = GALLERY_STATE.filters
    const loading = !GALLERY_STATE.allItemsComplete ||
        ((search || template) && GALLERY_STATE.classificationStatus === 'loading') ||
        (search && GALLERY_STATE.classificationTextStatus === 'loading') ||
        (minReactions > 0 && GALLERY_STATE.telegramStatus === 'loading')
    status.textContent = filtered
        ? `${totalEntries} Memes` + (GALLERY_STATE.allItemsError
            ? ' (Archiv unvollständig geladen. Ändere einen Filter, um es erneut zu versuchen.)' : '') +
            (loading ? ' (lade Memes...)' : '') +
            (search && GALLERY_STATE.classificationTextStatus === 'unavailable'
                ? ' (Volltextsuche nicht verfügbar)' : '') +
            (minReactions > 0 && GALLERY_STATE.telegramStatus === 'unavailable'
                ? ' (Reaktionen nicht verfügbar)' : '')
        : ''
    if (filtered && GALLERY_STATE.workerError) status.textContent = 'Suche nicht verfügbar. Bitte lade die Seite neu.'
    status.textContent += GALLERY_STATE.navigationNotice

    const scrollTop = document.documentElement.scrollTop
    const galleryTop = galleryNode.getBoundingClientRect ? galleryNode.getBoundingClientRect().top + scrollTop : 0
    const viewportHeight = document.documentElement.clientHeight || window.innerHeight || rowHeight
    // CSS backgrounds load as soon as they are rendered, even far offscreen.
    // Keep just one extra row on either side of the actual viewport.
    const firstRow = Math.min(Math.max(0, Math.floor((scrollTop - galleryTop) / rowHeight) - 1), Math.max(0, totalRows - 1))
    const endRow = Math.min(totalRows, Math.max(firstRow + 1,
        Math.ceil((scrollTop + viewportHeight - galleryTop) / rowHeight) + 1))
    const scrollEntry = firstRow * tnColumns
    const endEntry = Math.min(totalEntries, endRow * tnColumns)

    if (totalEntries === 0) {
        upgradeCandidates = []
        fittedClassifications.clear()
        galleryNode.innerHTML = ''
        return
    }

    const layout = { tnColumns, rowHeight, marginLeft, columnWidth, spriteScale }
    const renderState = scrollEntry + ":" + endEntry + ":" + tnColumns + ":" + galleryWidth + ":" + GALLERY_STATE.classificationStatus + ":" + GALLERY_STATE.classificationTextStatus + ":" + GALLERY_STATE.telegramStatus + ":" + version + ":" + galleryScrolling
    let ds = {
        dirStartIndex: scrollEntry,
        dataSourceItems: filtered ? GALLERY_STATE.filteredItems.slice(scrollEntry, endEntry)
            : Array.from({ length: endEntry - scrollEntry }, (_, i) => GALLERY_STATE.dataSource[scrollEntry + i]),
    }
    if (!filtered && ds.dataSourceItems.some(item => !item)) {
        // Show geometry immediately, even before an uncached month supplies dates/colors.
        if (galleryScrolling) renderGalleryItems(ds, layout, filtered, renderState + ':pending')
        let loaded
        try {
            loaded = await updateDataSources(scrollEntry)
        } catch (error) {
            console.warn('Could not load gallery month', error)
            return
        }
        // A scroll, resize, filter or newer render can supersede this request.
        if (renderVersion !== galleryRenderVersion || version !== GALLERY_STATE.filterVersion) return
        const offset = scrollEntry - loaded.dirStartIndex
        ds.dataSourceItems = loaded.dataSourceItems.slice(offset, offset + endEntry - scrollEntry)
    }
    return renderGalleryItems(ds, layout, filtered, renderState)
}

function renderGalleryItems(ds, layout, filtered, renderState) {
    const galleryNode = document.getElementById('gallery')
    const { tnColumns, rowHeight, marginLeft, columnWidth, spriteScale } = layout
    if (GALLERY_STATE.lastRenderState == renderState) {
        enhanceThumbnails(ds.dataSourceItems, filtered)
        return
    }

    GALLERY_STATE.lastRenderState = renderState

    // Reconcile the buffered window by identity, not by replacing its HTML.
    const cards = new Map(Array.from(galleryNode.children, card => [card.getAttribute('data-card-key'), card]))
    const renderedCards = []
    const nextClassifications = new Map()
    const preservedClassifications = new Set()
    const countFormat = new Intl.NumberFormat('de', { notation: 'compact', maximumFractionDigits: 1 })
    const formatCount = count => count === null ? "—" : countFormat.format(count)

    for (var i = 0; i < ds.dataSourceItems.length; i++) {
        var item = ds.dataSourceItems[i]
        const index = ds.dirStartIndex + i
        const galleryIndex = filtered ? index : item?.galleryIndex
        const itemStyles = [
            `top: ${Math.floor(index / tnColumns) * rowHeight}px;`,
            `left: ${marginLeft + (index % tnColumns) * columnWidth}px;`,
        ]
        const key = item ? item.imageId : `pending:${index}`
        let card = cards.get(key)
        if (!card) {
            card = document.createElement('article')
            card.className = 'gallery-item'
        }
        const positionStyle = itemStyles.join(' ')
        if (card.getAttribute('style') !== positionStyle) card.setAttribute('style', positionStyle)
        if (card.getAttribute('data-card-key') !== key) card.setAttribute('data-card-key', key)
        renderedCards.push(card)
        if (!item) {
            if (!cards.has(key)) {
                card.setAttribute('aria-busy', 'true')
                card.innerHTML = '<div class="thumbnail" style="background-color: #222;"></div>' +
                    '<div class="thumbnail-metadata">Datum wird geladen…</div>'
            }
            continue
        }
        const classification = GALLERY_STATE.classificationIndex[item.imageId]
        const classificationSource = classificationHTML(classification, item.imageId)
        const previous = fittedClassifications.get(item.imageId)
        // Keep the fitted buttons and overflow counter unchanged for overlapping cards.
        const preserve = previous?.html && previous.source === classificationSource && previous.scale === spriteScale
        const preview = preserve ? previous : { source: classificationSource, scale: spriteScale, html: null }
        nextClassifications.set(item.imageId, preview)
        if (preserve) preservedClassifications.add(item.imageId)
        const imageLabel = GALLERY_STATE.classificationTextIndex[item.imageId]?.description || `Bild vom ${item.date || 'unbekannten Datum'} öffnen`
        // Sprites are previews only: filters, unsupported WebP, failed sheets and
        // already-loaded originals all render archive images directly.
        const original = usesOriginal(item, filtered)
        const imageSrc = original ? item.src : item.thumbSrc
        // Scrolling may defer a NEW image, but must never erase an in-flight one.
        const thumbnail = card.querySelector('.thumbnail')
        const displayImage = !galleryScrolling || thumbnail?.galleryImageSrc === imageSrc ||
            GALLERY_IMAGES.get(imageSrc)?.status === 'loaded'
        const thumbStyles = !displayImage ? [] : original ? [
            `background-image: url('${item.src}');`,
            `background-position: center;`,
            `background-size: contain;`,
        ] : [
            `background-image: url('${item.thumbSrc}');`,
            `background-position: -${item.bgOffsetX * spriteScale}px -${item.bgOffsetY * spriteScale}px;`,
            // Scale the existing sheet, including its tile padding. Auto height
            // also handles partially filled sheets without changing generation.
            ...(spriteScale === 1 ? [] : [`background-size: ${THUMBNAIL_SHEET_WIDTH * spriteScale}px auto;`]),
        ]
        thumbStyles.push(`background-color: #${item.bg};`)
        const thumbAttrs = [
            `href="${item.src}"`,
            `class="thumbnail"`,
            `style="${thumbStyles.join(' ')}"`,
            `-data-gallery-idx="${galleryIndex}"`,
            `aria-label="${escapeHtml(imageLabel)}"`,
            `title="${escapeHtml(imageLabel)}"`,
        ]
        const reactionText = formatCount(item.reactions)
        const date = `<time datetime="${item.date || ''}">${item.date || 'Datum nicht verfügbar'}</time>`
        const telegramLink = item.telegramMessageId === null
            ? `<span class="telegram-unavailable" title="Telegram nicht verfügbar">${date}</span>`
            : `<a class="telegram-link" href="https://t.me/RosaroterPanzerBackup/${item.telegramMessageId}" target="_blank" rel="noopener" title="Auf Telegram ansehen">${date}</a>`

        const metadataDetailsHTML =
            `<div class="thumbnail-stats">` +
            `<span class="reaction-count" title="Telegram-Reaktionen" aria-label="${item.reactions ?? 'Nicht verfügbar'} Reaktionen">♥ ${reactionText}</span>` +
            (item.views === null ? '' : `<span title="Telegram-Aufrufe" aria-label="${item.views} Aufrufe">◉ ${formatCount(item.views)}</span>`) +
            (item.comments === null ? '' : `<span title="Telegram-Kommentare" aria-label="${item.comments} Kommentare">💬 ${formatCount(item.comments)}</span>`) +
            `</div>${telegramLink}` +
            (DEBUG_ENABLED ? `<button type="button" class="thumbnail-debug" data-gallery-idx="${galleryIndex}" aria-haspopup="dialog">debug</button>` : '')
        const metadataHTML = metadataDetailsHTML + classificationSource
        const renderedMetadataHTML = metadataDetailsHTML + (preview.html || classificationSource)
        if (!thumbnail) {
            card.innerHTML = `<a ${thumbAttrs.join(' ')}></a>` +
                `<div class="thumbnail-metadata">${renderedMetadataHTML}</div>`
        } else {
            // Keep the actual image-bearing node through scroll, resize and metadata
            // updates. In particular, do not rewrite its complete style attribute.
            for (const [name, value] of Object.entries({
                href: item.src, '-data-gallery-idx': galleryIndex,
                'aria-label': imageLabel, title: imageLabel,
            })) {
                if (thumbnail.getAttribute(name) !== String(value)) thumbnail.setAttribute(name, String(value))
            }
            if (card.galleryMetadataHTML !== metadataHTML) {
                card.querySelector('.thumbnail-metadata').innerHTML = renderedMetadataHTML
            }
        }
        if (displayImage) {
            paintGalleryThumbnail(thumbnail || card.querySelector('.thumbnail'), imageSrc,
                original ? 'center' : `-${item.bgOffsetX * spriteScale}px -${item.bgOffsetY * spriteScale}px`,
                original ? 'contain' : spriteScale === 1 ? '' : `${THUMBNAIL_SHEET_WIDTH * spriteScale}px auto`, item.bg)
        }
        card.galleryMetadataHTML = metadataHTML
    }

    closeTagOverlay()
    const retained = new Set(renderedCards)
    for (const card of Array.from(galleryNode.children)) {
        if (!retained.has(card)) card.remove()
    }
    for (let i = 0; i < renderedCards.length; i++) {
        const card = renderedCards[i]
        if (galleryNode.children[i] !== card) galleryNode.insertBefore(card, galleryNode.children[i] || null)
    }
    if (!galleryScrolling) {
        fitClassificationTags(galleryNode, preservedClassifications)
        for (const node of galleryNode.querySelectorAll('.thumbnail-classification[data-image-id]')) {
            const preview = nextClassifications.get(node.getAttribute('data-image-id'))
            if (preview) preview.html = node.outerHTML
        }
    }
    // Retain only the current window, not every card visited in the archive.
    fittedClassifications = nextClassifications
    enhanceThumbnails(ds.dataSourceItems.filter(Boolean), filtered)
    const firstItem = ds.dataSourceItems[0]
    return firstItem && (usesOriginal(firstItem, filtered) ? firstItem.src : firstItem.thumbSrc)
}

// Greedily try every tag. Rejected tags leave the flex flow so later, shorter
// tags can use the remaining space instead of being pushed into invisible rows.
function fitClassificationTags(galleryNode, preservedClassifications = new Set()) {
    for (const group of galleryNode.querySelectorAll('.classification-tags')) {
        const children = Array.from(group.children)
        const more = children.find(child => child.classList?.contains('classification-more'))
        if (preservedClassifications.has(more?.getAttribute('data-image-id'))) continue
        const tags = children.filter(child => child !== more)
        const bounds = group.getBoundingClientRect()
        const fitsBounds = rect => rect.bottom <= bounds.bottom + 0.5 &&
            rect.right <= bounds.right + 0.5 && rect.left >= bounds.left - 0.5
        const setCount = count => {
            more.textContent = `+${count}`
            more.setAttribute('aria-label', `${count} weitere Schlagwörter anzeigen`)
            more.setAttribute('title', `${count} weitere Schlagwörter anzeigen`)
        }
        const pack = reserveCounter => {
            let visible = 0
            for (const tag of tags) {
                tag.style.display = 'none'
                tag.style.visibility = 'hidden'
                tag.disabled = true
                tag.tabIndex = -1
            }
            if (reserveCounter) setCount(tags.length)
            for (const tag of tags) {
                // Overlay-only tags still contribute to the +N counter.
                if (tag.getAttribute?.('data-overlay-only') === 'true') continue
                tag.style.display = ''
                if (reserveCounter) setCount(tags.length - visible - 1)
                const fits = fitsBounds(tag.getBoundingClientRect()) &&
                    (!reserveCounter || fitsBounds(more.getBoundingClientRect()))
                tag.style.display = fits ? '' : 'none'
                tag.style.visibility = fits ? 'visible' : 'hidden'
                tag.disabled = !fits
                tag.tabIndex = fits ? 0 : -1
                if (fits) visible += 1
                if (reserveCounter) setCount(tags.length - visible)
            }
            return tags.length - visible
        }
        if (more) more.hidden = true
        const hiddenCount = pack(false)
        if (!more) continue
        more.hidden = hiddenCount === 0
        if (hiddenCount) {
            // The counter is the last flex item. Reserve its actual width while
            // trying tags, so it stays inline and counts every omitted item.
            pack(true)
        }
    }
}

let tagOverlay = null
let tagOverlayTrigger = null

function closeTagOverlay(restoreFocus = false) {
    if (!tagOverlay) return
    tagOverlay.remove()
    tagOverlay = null
    tagOverlayTrigger.setAttribute('aria-expanded', 'false')
    if (restoreFocus && tagOverlayTrigger.isConnected) tagOverlayTrigger.focus()
    tagOverlayTrigger = null
}

function showTagOverlay(trigger, event) {
    if (tagOverlayTrigger === trigger) {
        closeTagOverlay(true)
        return
    }
    closeTagOverlay()
    const classification = GALLERY_STATE.classificationIndex[trigger.getAttribute('data-image-id')]
    if (!classification) return
    const overlay = document.createElement('div')
    overlay.id = 'all-tags-overlay'
    overlay.setAttribute('role', 'dialog')
    overlay.setAttribute('aria-label', 'Schlagwörter zum Bild')
    const template = classification.template
        ? `<button type="button" class="classification-tag meme-template" data-template="${escapeHtml(classification.template)}">${escapeHtml(classification.template)}</button>` : ''
    overlay.innerHTML = '<button type="button" class="tags-overlay-close" aria-label="Schließen">×</button>' +
        '<div class="tags-overlay-list">' + template + classificationTags(classification).map(tag =>
            `<button type="button" class="classification-tag">${escapeHtml(tag)}</button>`).join('') + '</div>'
    document.body.appendChild(overlay)
    tagOverlay = overlay
    tagOverlayTrigger = trigger
    trigger.setAttribute('aria-expanded', 'true')
    // Fixed viewport coordinates keep the gallery geometry completely unchanged.
    const anchor = trigger.getBoundingClientRect()
    const x = event.detail !== 0 && Number.isFinite(event.clientX) ? event.clientX : anchor.right
    const groupTop = trigger.closest('.classification-tags').getBoundingClientRect().top
    const width = document.documentElement.clientWidth || window.innerWidth
    const height = document.documentElement.clientHeight || window.innerHeight
    // Size against the full viewport before measuring, not the space below the tags.
    overlay.style.maxWidth = Math.max(0, width - 16) + 'px'
    overlay.style.maxHeight = Math.max(0, Math.min(250, height - 16)) + 'px'
    const bounds = overlay.getBoundingClientRect()
    overlay.style.left = Math.max(8, Math.min(x + 12, width - bounds.width - 8)) + 'px'
    overlay.style.top = Math.max(8, Math.min(groupTop, height - bounds.height - 8)) + 'px'
    overlay.querySelector('.classification-tag, .tags-overlay-close').focus({ preventScroll: true })
}


async function preloadLightboxItems(index) {
    for (const nearbyIndex of [Math.max(index - 30, 0),
        Math.min(index + 30, GALLERY_STATE.dataSource.length - 1)]) {
        if (!GALLERY_STATE.dataSource[nearbyIndex]) await updateDataSources(nearbyIndex)
    }
}

function updateGalleryHandler(evt) {
    if (!GALLERY_STATE.dirNames) {return}  // not yet initialized
    closeTagOverlay()
    if (evt.type === 'scroll' && !window.lightbox.pswp) writeNavigation()
    galleryScrolling = true
    galleryRenderVersion += 1
    // Stop queued upgrades immediately; old in-flight loads may still complete.
    upgradeCandidates = []
    if (!galleryFramePending) {
        galleryFramePending = true
        requestAnimationFrame(() => {
            galleryFramePending = false
            updateGallery()
        })
    }
    clearTimeout(GALLERY_STATE.debounceTimeout)
    GALLERY_STATE.debounceTimeout = setTimeout(() => {
        galleryScrolling = false
        updateGallery()
    }, 150)
}


function showDebugMetadata(item) {
    let dialog = document.getElementById('image-debug-dialog')
    if (!dialog) {
        dialog = document.createElement('dialog')
        dialog.id = 'image-debug-dialog'
        dialog.setAttribute('aria-labelledby', 'image-debug-title')
        dialog.innerHTML = '<form method="dialog"><button type="submit" autofocus>Schließen</button></form>' +
            '<h2 id="image-debug-title">Bild-Metadaten</h2><pre></pre>'
        dialog.addEventListener('click', event => {
            if (event.target === dialog) {
                const bounds = dialog.getBoundingClientRect()
                if (event.clientX < bounds.left || event.clientX > bounds.right ||
                    event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close()
            }
        })
        document.body.appendChild(dialog)
    }
    const { entryMetadata, ...galleryItem } = item
    const filename = item.imageId.split('/').pop()
    dialog.querySelector('pre').textContent = JSON.stringify({
        imageId: item.imageId,
        entry: entryMetadata,
        telegram: GALLERY_STATE.telegramMetadata[filename] ?? null,
        classificationStatus: GALLERY_STATE.classificationStatus,
        classification: GALLERY_STATE.classificationIndex[item.imageId] ?? null,
        classificationTextStatus: GALLERY_STATE.classificationTextStatus,
        classificationText: GALLERY_STATE.classificationTextIndex[item.imageId] ?? null,
        gallery: galleryItem,
    }, null, 2)
    dialog.showModal()
}

function galleryClickHandler(evt) {
    const more = evt.target.closest?.('.classification-more') ||
        (evt.target.classList.contains('classification-more') ? evt.target : null)
    if (more) {
        evt.preventDefault()
        showTagOverlay(more, evt)
        return false
    }
    if (evt.target.classList.contains('tags-overlay-close')) {
        evt.preventDefault()
        closeTagOverlay(true)
        return false
    }
    if (tagOverlay && !tagOverlay.contains(evt.target)) closeTagOverlay()
    if (DEBUG_ENABLED && evt.target.classList.contains('thumbnail-debug')) {
        evt.preventDefault()
        const index = Number(evt.target.getAttribute('data-gallery-idx'))
        const items = filtersActive() ? GALLERY_STATE.filteredItems : GALLERY_STATE.dataSource
        if (items?.[index]) showDebugMetadata(items[index])
        return false
    }
    if (evt.target.classList.contains('classification-tag')) {
        evt.preventDefault()
        const isTemplate = evt.target.classList.contains('meme-template')
        const control = document.getElementById(isTemplate ? 'filter-template' : 'filter-search')
        const value = isTemplate ? evt.target.getAttribute('data-template') : evt.target.textContent.trim()
        if (isTemplate && !Array.from(control.options).some(option => option.value === value)) {
            const count = GALLERY_STATE.templateCounts.get(value) || 0
            control.insertAdjacentHTML('beforeend', templateOptionHTML(value, count))
        }
        control.value = value
        if (isTemplate) control.focus()
        filterChangeHandler()
        return false
    }
    if (!evt.target.classList.contains('thumbnail')) {return}
    evt.preventDefault()

    const galleryIndex = parseInt(evt.target.getAttribute('-data-gallery-idx'), 10)

    const dataSource = filtersActive() ? GALLERY_STATE.filteredItems : GALLERY_STATE.dataSource
    if (!dataSource?.[galleryIndex]) return false
    // Save the gallery position before adding an image-specific history entry.
    writeNavigation()
    navigationVersion += 1
    restoringNavigation = false
    selectedImageId = dataSource[galleryIndex].imageId
    GALLERY_STATE.navigationNotice = ''
    writeNavigation(true)
    window.lightbox.options.dataSource = dataSource
    window.lightbox.loadAndOpen(galleryIndex)
    return false
}

function initHandlers() {
    history.scrollRestoration = 'manual'
    window.addEventListener('popstate', () => { restoreNavigation(readNavigation()) })
    window.lightbox.on('change', lightboxChangeHandler)
    window.lightbox.on('close', lightboxCloseHandler)
    for (const id of ['filter-reactions', 'filter-template']) {
        document.getElementById(id).addEventListener('input', filterChangeHandler)
    }
    document.getElementById('filter-search').addEventListener('input', searchInputHandler)
    document.getElementById('filter-search-clear').addEventListener('click', () => {
        document.getElementById('filter-search').value = ''
        filterChangeHandler()
    })
    window.addEventListener('scroll', updateGalleryHandler)
    window.addEventListener('resize', updateGalleryHandler)
    window.addEventListener('click', galleryClickHandler)
    window.addEventListener('keydown', event => {
        if (event.key === 'Escape' && tagOverlay) {
            event.preventDefault()
            closeTagOverlay(true)
        }
    })
}

async function initGallery() {
    const [dirIndex, webpSupported] = await Promise.all([
        fetchJson("images/dir_index.json"), supportsWebp(),
    ])
    GALLERY_STATE.dirIndex = dirIndex
    GALLERY_STATE.webpSupported = webpSupported
    GALLERY_STATE.dirNames = []

    GALLERY_STATE.totalEntries = 0;

    Object.entries(GALLERY_STATE.dirIndex).forEach(([dirName, numEntries]) => {
      // console.log(`dirName: ${dirName}, numEntries: ${numEntries}`);
      GALLERY_STATE.totalEntries += numEntries
      GALLERY_STATE.dirNames.push(dirName)
    });

    GALLERY_STATE.dirNames.sort()
    GALLERY_STATE.dataSource = [
        // {src: '...', width: ..., height: ...},
    ]
    GALLERY_STATE.dataSource.length = GALLERY_STATE.totalEntries

    initHandlers()
    const restoration = restoreNavigation(readNavigation())
    const firstThumbSrc = await updateGallery()
    resolveGalleryReady()
    // Neither metadata request may delay the first gallery render.
    await Promise.all([loadTelegramMetadata(firstThumbSrc), loadClassifications()])
    // The large OCR/description index starts last, after other metadata is rendered.
    await loadClassificationText()
    resolveMetadataReady()
    await restoration
}

initGallery()

window.panzerApp = {
    updateGalleryHandler: updateGalleryHandler
}

})();
