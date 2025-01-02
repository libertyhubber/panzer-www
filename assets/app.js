(function(){
"strict";

const THUMBNAIL_SIZE = 150
const THUMBNAIL_MARGIN = 16
const THUMBNAIL_MSIZE = THUMBNAIL_SIZE + THUMBNAIL_MARGIN

// const CB = Math.random().toString(36).slice(2); // Cachebust
const CB = parseInt(+new Date() / (1000 * 3600)).toString(36)

const CACHE = {}

const IMG_HOSTS = {};

if (!location.host.startsWith("localhost")) {
    Object.assign(IMG_HOSTS, {
        "2021": "archiv0.derrosarotepanzer.com",
        "2022": "archiv0.derrosarotepanzer.com",
        "2023": "archiv0.derrosarotepanzer.com",
        "2024": "archiv0.derrosarotepanzer.com",
        "2025": "archiv1.derrosarotepanzer.com",
        "2026": "archiv1.derrosarotepanzer.com",
        "2027": "archiv2.derrosarotepanzer.com",
        "2028": "archiv2.derrosarotepanzer.com",
        "2029": "archiv3.derrosarotepanzer.com",
        "2030": "archiv3.derrosarotepanzer.com",
        "2031": "archiv4.derrosarotepanzer.com",
        "2032": "archiv4.derrosarotepanzer.com",
        "2033": "archiv5.derrosarotepanzer.com",
        "2034": "archiv5.derrosarotepanzer.com",
    });
}

async function fetchJson(path) {
    if (!CACHE[path]) {
        const promise = await fetch(path)
        CACHE[path] = await promise.json()
    }
    return CACHE[path]
}

const GALLERY_STATE = {
    'dirNames': null, // [dirName, ....]
    'dirIndex': null, // {dirName: numEntries, ....}
    'totalEntries': -1,
    'debounceTimeout': null,
    'lastRenderState': null,
    'dataSource': null,
}

async function updateDataSources(itemIndex) {
    // scan through directories
    var dirStartIndex = 0
    for (var dirCursor = GALLERY_STATE.dirNames.length - 1; dirCursor >= 0; dirCursor--) {
        var dirEntryCount = GALLERY_STATE.dirIndex[GALLERY_STATE.dirNames[dirCursor]]
        if (itemIndex > dirStartIndex + dirEntryCount) {
            dirStartIndex += dirEntryCount
        } else {
            break
        }
    }

    const dirNames = []
    const entryPromises = []
    const thumbSrcs = []

    const fallbackHost = location.protocol + "//" + location.host

    for (var i = dirCursor; i >= Math.max(0, dirCursor - 1); i--) {
        var dirName = GALLERY_STATE.dirNames[i]
        dirNames.push(dirName)

        var host = IMG_HOSTS[dirName.split("/")[0]] || fallbackHost;

        var dirURL = `${host}/images/${dirName}/entry_index.json?cb=${CB}`
        entryPromises.push(fetchJson(dirURL))

        var thumbSrc = `${host}/images/${dirName}/thumbnails.jpg?cb=${CB}`
        thumbSrcs.push(thumbSrc)

        new Image().src = thumbSrc;
    }

    const entryIndexes = await Promise.all(entryPromises)

    const dataSourceItems = []

    for (var i = 0; i < entryIndexes.length; i++) {
        var dirName = dirNames[i]
        var entryIndex = entryIndexes[i]
        var thumbSrc = thumbSrcs[i]

        var host = IMG_HOSTS[dirName.split("/")[0]] || fallbackHost;

        for (var j = entryIndex.length - 1; j >= 0; j--) {
            var entry = entryIndex[j]
            dataSourceItems.push({
                src: `${host}/images/${dirName}/${entry.name}`,
                width: entry.w,
                height: entry.h,
                bgOffsetX: entry.x,
                bgOffsetY: entry.y,
                thumbSrc: thumbSrc,
                galleryIndex: dirStartIndex + dataSourceItems.length,
            })
        }
    }

    for (var i = dataSourceItems.length - 1; i >= 0; i--) {
        GALLERY_STATE.dataSource[dirStartIndex + i] = dataSourceItems[i]
    }

    return {
       dirCursor: dirCursor,
       dirStartIndex: dirStartIndex,
       dataSourceItems: dataSourceItems,
    }
}


async function updateGallery() {
    if (!GALLERY_STATE.dirNames) {return}  // not yet initialized

    const galleryNode = document.getElementById("gallery")

    const tnColumns = Math.floor(galleryNode.clientWidth / THUMBNAIL_MSIZE)
    const marginLeft = Math.round((galleryNode.clientWidth - (tnColumns * THUMBNAIL_MSIZE)) / 2)

    const totalRows = Math.ceil(GALLERY_STATE.totalEntries / tnColumns)
    galleryNode.style.height = (totalRows * THUMBNAIL_MSIZE) + "px"

    const scrollTop = document.documentElement.scrollTop
    const scrollRow = Math.max(0, Math.floor(scrollTop / THUMBNAIL_MSIZE) - 9)
    const scrollEntry = scrollRow * tnColumns

    const ds = await updateDataSources(scrollEntry)

    const renderState = ds.dirCursor + ":" + tnColumns + ":" + parseInt(window.innerWidth / 10)

    if (GALLERY_STATE.lastRenderState == renderState) {
        return
    }

    GALLERY_STATE.lastRenderState = renderState

    var entryRow = 0
    var entryCol = ds.dirStartIndex % tnColumns

    const dirOffsetTop = Math.round(((ds.dirStartIndex - entryCol) / tnColumns) * THUMBNAIL_MSIZE)

    const thumbnailsHTML = []

    for (var i = 0; i < ds.dataSourceItems.length; i++) {
        var item = ds.dataSourceItems[i]

        const offsetTop = dirOffsetTop + (entryRow * THUMBNAIL_MSIZE)
        const offsetLeft = marginLeft + entryCol * THUMBNAIL_MSIZE

        const thumbStyles = [
            `top: ${offsetTop}px;`,
            `left: ${offsetLeft}px;`,
            `background-image: url('${item.thumbSrc}');`,
            // `background-size: ${THUMBNAIL_SIZE}px ${THUMBNAIL_SIZE}px;`,
            `background-position: -${item.bgOffsetX}px -${item.bgOffsetY}px;`,
        ]

        const thumbAttrs = [
            `href="${item.src}"`,
            `class="thumbnail"`,
            `style="${thumbStyles.join(' ')}"`,
            `-data-gallery-idx="${item.galleryIndex}"`,
        ]

        // TODO (mb 2024-05-04): maybe add <img>
        //          and see if the animation works.
        thumbnailsHTML.push(`<a ${thumbAttrs.join(' ')}></a>`)

        entryCol += 1
        if (entryCol >= tnColumns) {
            entryRow += 1
            entryCol = 0
        }
    }

    galleryNode.innerHTML = thumbnailsHTML.join("")
}


async function updateGalleryHandler(evt) {
    if (!GALLERY_STATE.dirNames) {return}  // not yet initialized

    if (evt.constructor.name == 'PhotoSwipeEvent') {
        const lookBackIndex = Math.max(lightbox.pswp.currIndex - 30, 0)
        if (!GALLERY_STATE.dataSource[lookBackIndex]) {
            await updateDataSources(lookBackIndex)
        }
        const lookAheadIndex = Math.min(lightbox.pswp.currIndex + 30, GALLERY_STATE.dataSource.length - 1)

        if (!GALLERY_STATE.dataSource[lookAheadIndex]) {
            await updateDataSources(lookAheadIndex)
        }
    } else {
        clearTimeout(GALLERY_STATE.debounceTimeout)
        GALLERY_STATE.debounceTimeout = setTimeout(updateGallery, 150)
    }
}


async function initGallery() {
    GALLERY_STATE.dirIndex = await fetchJson("images/dir_index.json")
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

    updateGallery()
}

function galleryClickHandler(evt) {
    if (!evt.target.classList.contains('thumbnail')) {return}
    evt.preventDefault()

    const galleryIndex = parseInt(evt.target.getAttribute('-data-gallery-idx'), 10)

    window.lightbox.options.dataSource = GALLERY_STATE.dataSource
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
    window.addEventListener('scroll', updateGalleryHandler)
    window.addEventListener('resize', updateGalleryHandler)
    window.addEventListener('click', galleryClickHandler)
    window.addEventListener('click', navClickHandler)
}

function initApp() {
    initGallery()
    initHandlers()
}
initApp()

window.panzerApp = {
    updateGalleryHandler: updateGalleryHandler
}

})();
