(function(){
"strict";

// Keep sprite geometry in sync with scripts/generate_thumbnails.py and style.css.
const THUMBNAIL_SIZE = 220
const IMAGES_PER_SHEET = 20
const SHEET_COLUMNS = 5
const THUMBNAIL_PADDING = 2
const THUMBNAIL_MARGIN = 16
const THUMBNAIL_WIDTH = THUMBNAIL_SIZE + THUMBNAIL_MARGIN
const THUMBNAIL_ROW_HEIGHT = 364
const DEBUG_ENABLED = new URL(location.href).searchParams.get('debug') === '1'
const ORIGINAL_LOAD_LIMIT = 4
const GALLERY_IMAGES = new Map()
let upgradeCandidates = []
let activeOriginalLoads = 0

const GALLERY_STATE = {
    'webpSupported': false,
    'dirIndex': null, // {dirName: numEntries, ....}
    'dirNames': null, // [dirName, ....]
    'telegramMetadata': {}, // {filename: [messageId, reactionCount, views, comments], ...}
    'telegramStatus': 'loading',
    'classificationIndex': {}, // {"YYYY/MM/filename": {tags, template}}
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
    'allItemsComplete': false,
    'allItemsError': false,
    'filteredItems': null,
    'filters': { minReactions: 0, template: '', search: '' },
    'filterVersion': 0,
    'navigationNotice': '',
}

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
    closeTagOverlay()
    const lightboxClosed = cancelLightbox()
    selectedImageId = navigation.image
    GALLERY_STATE.navigationNotice = ''
    appliedSearch = navigation.search
    document.getElementById('filter-reactions').value = navigation.minReactions
    document.getElementById('filter-search').value = navigation.search
    GALLERY_STATE.filters = {
        minReactions: navigation.minReactions,
        template: navigation.template,
        search: normalizeSearch(navigation.search),
    }
    setTemplateControl(navigation.template)
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

async function loadMonthItems(dirName, dirStartIndex) {
    // Local indexes and sprites are generated together; only originals use IMG_HOSTS.
    const entryIndex = await fetchJson(`images/${dirName}/entry_index.json`)
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

function countCatalogTags(index) {
    const counts = new Map()
    for (const classification of Object.values(index)) {
        // Count images, not duplicate tags within the same image.
        for (const key of new Set(classification.tags.map(tagKey))) {
            if (key) counts.set(key, (counts.get(key) || 0) + 1)
        }
    }
    return counts
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
    const frequency = tag => GALLERY_STATE.tagCounts.get(tagKey(tag)) || 0
    const sortedTags = [...classification.tags]
        .sort((a, b) => frequency(b) - frequency(a) || a.localeCompare(b, 'de'))
    const tags = sortedTags
        .map(tag => `<button type="button" class="classification-tag">${escapeHtml(tag)}</button>`)
        .join('')
    return `<div class="thumbnail-classification">` +
        (template || classification.tags.length ?
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

function showOriginal(src) {
    // Only mutate currently rendered links; async loads may outlive a filter or scroll.
    for (const thumbnail of document.getElementById('gallery').querySelectorAll('.thumbnail')) {
        if (thumbnail.getAttribute('href') !== src) continue
        thumbnail.style.backgroundImage = `url('${src}')`
        thumbnail.style.backgroundPosition = 'center'
        thumbnail.style.backgroundSize = 'contain'
        thumbnail.style.backgroundColor = 'black'
    }
}

function loadOriginals() {
    while (activeOriginalLoads < ORIGINAL_LOAD_LIMIT) {
        const item = upgradeCandidates.find(item =>
            !GALLERY_IMAGES.has(item.src) && GALLERY_IMAGES.get(item.thumbSrc)?.painted)
        if (!item) break
        activeOriginalLoads += 1
        galleryImage(item.src, 'low').promise.then(loaded => {
            activeOriginalLoads -= 1
            if (loaded) showOriginal(item.src)
            // A failed original leaves its sprite preview intact, without retry loops.
            loadOriginals()
        })
    }
}

function enhanceThumbnails(items, filtered) {
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
                    if (item.thumbSrc === src) showOriginal(item.src)
                }
            }
            loadOriginals()
        })
    }
    loadOriginals()
}

async function loadTelegramMetadata(firstThumbSrc) {
    // Share the first image's load/paint gate with lazy quality enhancement.
    if (firstThumbSrc) await galleryImagePaint(firstThumbSrc)
    else await afterImagePaint()
    try {
        GALLERY_STATE.telegramMetadata = await fetchJson('images/telegram_metadata.json')
        GALLERY_STATE.telegramStatus = 'ready'
        for (const item of GALLERY_STATE.dataSource) {
            if (item) Object.assign(item, telegramFields(item.imageId.split('/').pop()))
        }
    } catch (error) {
        GALLERY_STATE.telegramStatus = 'unavailable'
        console.warn('Could not load Telegram metadata', error)
    }
    GALLERY_STATE.filteredItems = null
    GALLERY_STATE.lastRenderState = null
    await updateGallery()
}

async function loadClassifications() {
    try {
        GALLERY_STATE.classificationIndex = await fetchJson('images/classification_index.json')
        GALLERY_STATE.tagCounts = countCatalogTags(GALLERY_STATE.classificationIndex)
        GALLERY_STATE.classificationStatus = 'ready'
    } catch (error) {
        GALLERY_STATE.classificationStatus = 'unavailable'
        console.warn('Could not load image classifications', error)
    }
    const select = document.getElementById('filter-template')
    const templateCounts = new Map()
    for (const item of Object.values(GALLERY_STATE.classificationIndex)) {
        if (item.template) templateCounts.set(item.template, (templateCounts.get(item.template) || 0) + 1)
    }
    GALLERY_STATE.templateCounts = templateCounts
    const templates = [...templateCounts.entries()]
        .filter(([, count]) => count >= 5)
        .sort(([a], [b]) => a.localeCompare(b))
    select.innerHTML = '<option value="">Alle Vorlagen</option>' + templates.map(([name, count]) =>
        templateOptionHTML(name, count)).join('')
    setTemplateControl(GALLERY_STATE.filters.template)
    select.disabled = GALLERY_STATE.classificationStatus !== 'ready'
    GALLERY_STATE.filteredItems = null
    await updateGallery()
}

async function loadClassificationText() {
    try {
        GALLERY_STATE.classificationTextIndex = await fetchJson('images/classification_text_index.json')
        GALLERY_STATE.classificationTextStatus = 'ready'
    } catch (error) {
        GALLERY_STATE.classificationTextStatus = 'unavailable'
        console.warn('Could not load image classification text', error)
    }
    const search = document.getElementById('filter-search')
    search.placeholder = GALLERY_STATE.classificationTextStatus === 'ready'
        ? 'Bildtexte, Beschreibungen und Schlagwörter durchsuchen'
        : 'Schlagwörter durchsuchen (Volltextsuche nicht verfügbar)'
    GALLERY_STATE.filteredItems = null
    GALLERY_STATE.lastRenderState = null
    await updateGallery()
}

function filtersActive() {
    const filters = GALLERY_STATE.filters
    return filters.minReactions > 0 || filters.template !== '' || filters.search !== ''
}

async function loadAllItems() {
    if (!GALLERY_STATE.allItemsPromise) {
        GALLERY_STATE.allItemsError = false
        const refresh = () => {
            GALLERY_STATE.filteredItems = null
            GALLERY_STATE.lastRenderState = null
            if (filtersActive()) updateGallery()
        }
        GALLERY_STATE.allItemsPromise = (async () => {
            const months = []
            let offset = 0
            for (const dirName of [...GALLERY_STATE.dirNames].reverse()) {
                if (GALLERY_STATE.dirIndex[dirName] > 0) months.push({ dirName, start: offset })
                offset += GALLERY_STATE.dirIndex[dirName]
            }
            let cursor = 0
            // Each slot handles one month, independently of neighboring requests.
            await Promise.all(Array.from({ length: Math.min(6, months.length) }, async () => {
                while (cursor < months.length) {
                    const { dirName, start } = months[cursor++]
                    try {
                        await loadMonthItems(dirName, start)
                    } catch (error) {
                        GALLERY_STATE.allItemsError = true
                        console.warn(`Could not load archive month ${dirName}`, error)
                    }
                    refresh()
                }
            }))
            GALLERY_STATE.allItemsComplete = !GALLERY_STATE.allItemsError
            refresh()
        })().catch(error => {
            GALLERY_STATE.allItemsError = true
            console.warn('Could not load archive for filtering', error)
            refresh()
        })
    }
    await GALLERY_STATE.allItemsPromise
}

function normalizeSearch(value) {
    return value.replace(/\s+/g, ' ').trim().toLowerCase()
}

function matchesFilters(item) {
    const filters = GALLERY_STATE.filters
    if (filters.minReactions > 0 && (item.reactions ?? 0) < filters.minReactions) return false
    const classification = GALLERY_STATE.classificationIndex[item.imageId]
    if (filters.template && classification?.template !== filters.template) return false
    if (filters.search) {
        const details = GALLERY_STATE.classificationTextIndex[item.imageId]
        const text = normalizeSearch([details?.text, details?.description,
            classification?.template, ...(classification?.tags || [])].filter(Boolean).join(' '))
        if (!text.includes(filters.search)) return false
    }
    return true
}

function searchInputHandler() {
    clearTimeout(GALLERY_STATE.searchDebounceTimeout)
    GALLERY_STATE.searchDebounceTimeout = setTimeout(filterChangeHandler, 200)
}

function filterChangeHandler() {
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
    if (GALLERY_STATE.allItemsError) GALLERY_STATE.allItemsPromise = null
    GALLERY_STATE.filterVersion += 1
    upgradeCandidates = []
    document.getElementById('gallery').innerHTML = ''
    GALLERY_STATE.filteredItems = null
    GALLERY_STATE.lastRenderState = null
    clearTimeout(GALLERY_STATE.debounceTimeout)
    GALLERY_STATE.debounceTimeout = setTimeout(updateGallery, 150)
}

async function updateGallery() {
    if (!GALLERY_STATE.dirNames) {return}  // not yet initialized

    const galleryNode = document.getElementById("gallery")

    const tnColumns = Math.max(1, Math.floor(galleryNode.clientWidth / THUMBNAIL_WIDTH))
    const marginLeft = Math.round((galleryNode.clientWidth - (tnColumns * THUMBNAIL_WIDTH)) / 2)

    const version = GALLERY_STATE.filterVersion
    const filtered = filtersActive()
    const status = document.getElementById('filter-status')
    if (filtered && !GALLERY_STATE.filteredItems) {
        // Start the bounded background loader, but render already available matches.
        loadAllItems()
        GALLERY_STATE.filteredItems = GALLERY_STATE.dataSource.filter(matchesFilters)
            .map((item, galleryIndex) => ({ ...item, galleryIndex }))
    }
    const totalEntries = filtered ? GALLERY_STATE.filteredItems.length : GALLERY_STATE.totalEntries
    const totalRows = Math.ceil(totalEntries / tnColumns)
    galleryNode.style.height = (totalRows * THUMBNAIL_ROW_HEIGHT) + "px"
    status.textContent = filtered
        ? `${totalEntries} passende Bilder` + (GALLERY_STATE.allItemsError
            ? ' (Archiv unvollständig geladen. Ändere einen Filter, um es erneut zu versuchen.)'
            : !GALLERY_STATE.allItemsComplete ? ' (Archiv wird geladen…)' : '') + (GALLERY_STATE.filters.search && GALLERY_STATE.classificationStatus !== 'ready'
            ? ' (Schlagwortsuche benötigt Klassifizierungsdaten)' : '') + (GALLERY_STATE.filters.search && GALLERY_STATE.classificationTextStatus !== 'ready'
            ? GALLERY_STATE.classificationTextStatus === 'loading' ? ' (Volltextsuche wird geladen…)' : ' (Volltextsuche nicht verfügbar)' : '') + (GALLERY_STATE.filters.minReactions > 0 && GALLERY_STATE.telegramStatus !== 'ready'
            ? GALLERY_STATE.telegramStatus === 'loading' ? ' (Reaktionen werden geladen…)' : ' (Reaktionen nicht verfügbar)' : '')
        : ''
    status.textContent += GALLERY_STATE.navigationNotice

    const scrollTop = document.documentElement.scrollTop
    const galleryTop = galleryNode.getBoundingClientRect ? galleryNode.getBoundingClientRect().top + scrollTop : 0
    const viewportHeight = document.documentElement.clientHeight || window.innerHeight || THUMBNAIL_ROW_HEIGHT
    // CSS backgrounds load as soon as they are rendered, even far offscreen.
    // Keep just one extra row on either side of the actual viewport.
    const firstRow = Math.min(Math.max(0, Math.floor((scrollTop - galleryTop) / THUMBNAIL_ROW_HEIGHT) - 1), Math.max(0, totalRows - 1))
    const endRow = Math.min(totalRows, Math.max(firstRow + 1,
        Math.ceil((scrollTop + viewportHeight - galleryTop) / THUMBNAIL_ROW_HEIGHT) + 1))
    const scrollEntry = firstRow * tnColumns
    const endEntry = Math.min(totalEntries, endRow * tnColumns)

    if (totalEntries === 0) {
        upgradeCandidates = []
        galleryNode.innerHTML = ''
        return
    }

    let ds = filtered ? {
        dirCursor: scrollEntry,
        dirStartIndex: scrollEntry,
        dataSourceItems: GALLERY_STATE.filteredItems.slice(scrollEntry, endEntry),
    } : await updateDataSources(scrollEntry)
    if (version !== GALLERY_STATE.filterVersion) return

    // Render a bounded window, not two entire months worth of sprite downloads.
    if (!filtered) {
        const offset = scrollEntry - ds.dirStartIndex
        ds = {
            ...ds,
            dirStartIndex: scrollEntry,
            dataSourceItems: ds.dataSourceItems.slice(offset, offset + endEntry - scrollEntry),
        }
    }

    const renderState = scrollEntry + ":" + endEntry + ":" + tnColumns + ":" + parseInt(window.innerWidth / 10) + ":" + GALLERY_STATE.classificationStatus + ":" + GALLERY_STATE.classificationTextStatus + ":" + GALLERY_STATE.telegramStatus + ":" + version

    if (GALLERY_STATE.lastRenderState == renderState) {
        enhanceThumbnails(ds.dataSourceItems, filtered)
        return
    }

    GALLERY_STATE.lastRenderState = renderState

    var entryRow = 0
    var entryCol = ds.dirStartIndex % tnColumns

    const dirOffsetTop = Math.round(((ds.dirStartIndex - entryCol) / tnColumns) * THUMBNAIL_ROW_HEIGHT)

    const thumbnailsHTML = []
    const countFormat = new Intl.NumberFormat('de', { notation: 'compact', maximumFractionDigits: 1 })
    const formatCount = count => count === null ? "—" : countFormat.format(count)

    for (var i = 0; i < ds.dataSourceItems.length; i++) {
        var item = ds.dataSourceItems[i]
        const classification = GALLERY_STATE.classificationIndex[item.imageId]
        const imageLabel = GALLERY_STATE.classificationTextIndex[item.imageId]?.description || `Bild vom ${item.date || 'unbekannten Datum'} öffnen`

        const offsetTop = dirOffsetTop + (entryRow * THUMBNAIL_ROW_HEIGHT)
        const offsetLeft = marginLeft + entryCol * THUMBNAIL_WIDTH

        const itemStyles = [
            `top: ${offsetTop}px;`,
            `left: ${offsetLeft}px;`,
        ]
        // Sprites are previews only: filters, unsupported WebP, failed sheets and
        // already-loaded originals all render archive images directly.
        const thumbStyles = usesOriginal(item, filtered) ? [
            `background-image: url('${item.src}');`,
            `background-position: center;`,
            `background-size: contain;`,
            `background-color: black;`,
        ] : [
            `background-image: url('${item.thumbSrc}');`,
            `background-position: -${item.bgOffsetX}px -${item.bgOffsetY}px;`,
        ]
        const thumbAttrs = [
            `href="${item.src}"`,
            `class="thumbnail"`,
            `style="${thumbStyles.join(' ')}"`,
            `-data-gallery-idx="${item.galleryIndex}"`,
            `aria-label="${escapeHtml(imageLabel)}"`,
            `title="${escapeHtml(imageLabel)}"`,
        ]
        const reactionText = formatCount(item.reactions)
        const date = `<time datetime="${item.date || ''}">${item.date || 'Datum nicht verfügbar'}</time>`
        const telegramLink = item.telegramMessageId === null
            ? `<span class="telegram-unavailable" title="Telegram nicht verfügbar">${date}</span>`
            : `<a class="telegram-link" href="https://t.me/RosaroterPanzerBackup/${item.telegramMessageId}" target="_blank" rel="noopener" title="Auf Telegram ansehen">${date}</a>`

        thumbnailsHTML.push(
            `<article class="gallery-item" style="${itemStyles.join(' ')}">` +
            `<a ${thumbAttrs.join(' ')}></a>` +
            `<div class="thumbnail-metadata">` +
            `<div class="thumbnail-stats">` +
            `<span class="reaction-count" title="Telegram-Reaktionen" aria-label="${item.reactions ?? 'Nicht verfügbar'} Reaktionen">♥ ${reactionText}</span>` +
            (item.views === null ? '' : `<span title="Telegram-Aufrufe" aria-label="${item.views} Aufrufe">◉ ${formatCount(item.views)}</span>`) +
            (item.comments === null ? '' : `<span title="Telegram-Kommentare" aria-label="${item.comments} Kommentare">💬 ${formatCount(item.comments)}</span>`) +
            `</div>${telegramLink}` +
            (DEBUG_ENABLED ? `<button type="button" class="thumbnail-debug" data-gallery-idx="${item.galleryIndex}" aria-haspopup="dialog">debug</button>` : '') +
            classificationHTML(classification, item.imageId) +
            `</div></article>`
        )

        entryCol += 1
        if (entryCol >= tnColumns) {
            entryRow += 1
            entryCol = 0
        }
    }

    closeTagOverlay()
    galleryNode.innerHTML = thumbnailsHTML.join("")
    fitClassificationTags(galleryNode)
    enhanceThumbnails(ds.dataSourceItems, filtered)
    const firstItem = ds.dataSourceItems[0]
    return firstItem && (usesOriginal(firstItem, filtered) ? firstItem.src : firstItem.thumbSrc)
}

// Greedily try every tag. Rejected tags leave the flex flow so later, shorter
// tags can use the remaining space instead of being pushed into invisible rows.
function fitClassificationTags(galleryNode) {
    for (const group of galleryNode.querySelectorAll('.classification-tags')) {
        const children = Array.from(group.children)
        const more = children.find(child => child.classList?.contains('classification-more'))
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
            for (const tag of tags) tag.style.display = 'none'
            if (reserveCounter) setCount(tags.length)
            for (const tag of tags) {
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
        '<div class="tags-overlay-list">' + template + classification.tags.map(tag =>
            `<button type="button" class="classification-tag">${escapeHtml(tag)}</button>`).join('') + '</div>'
    document.body.appendChild(overlay)
    tagOverlay = overlay
    tagOverlayTrigger = trigger
    trigger.setAttribute('aria-expanded', 'true')
    // Fixed viewport coordinates keep the gallery geometry completely unchanged.
    const anchor = trigger.getBoundingClientRect()
    const x = event.detail !== 0 && Number.isFinite(event.clientX) ? event.clientX : anchor.right
    const groupTop = trigger.closest('.classification-tags').getBoundingClientRect().top
    const bounds = overlay.getBoundingClientRect()
    const width = document.documentElement.clientWidth || window.innerWidth
    const height = document.documentElement.clientHeight || window.innerHeight
    overlay.style.left = Math.max(8, Math.min(x + 12, width - bounds.width - 8)) + 'px'
    const top = Math.max(8, Math.min(groupTop, height - 8))
    overlay.style.top = top + 'px'
    overlay.style.maxHeight = Math.min(360, height - top - 8) + 'px'
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
    clearTimeout(GALLERY_STATE.debounceTimeout)
    GALLERY_STATE.debounceTimeout = setTimeout(updateGallery, 150)
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
        control.focus()
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

function navClickHandler(evt) {
    if (!evt.target.nodeName == 'SPAN') {return}
    if (evt.target.classList.contains('socials')) {
        if (evt.target.classList.contains('active')) {
            evt.target.classList.remove('active')
        } else {
            evt.target.classList.add('active')
        }
    } else {
        const node = document.querySelector(".socials.active")
        node && node.classList.remove('active')
    }
    if (evt.target.classList.contains('support')) {
        if (evt.target.classList.contains('active')) {
            evt.target.classList.remove('active')
        } else {
            evt.target.classList.add('active')
        }
    } else {
        const node = document.querySelector(".support.active")
        node && node.classList.remove('active')
    }
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
    window.addEventListener('scroll', updateGalleryHandler)
    window.addEventListener('resize', updateGalleryHandler)
    window.addEventListener('click', galleryClickHandler)
    window.addEventListener('click', navClickHandler)
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
