'use strict';

// All archive I/O, normalization and matching happen here, never in a render task.
let baseURL, cacheBust
const requests = new Map()
const values = new Map()
const records = []
const indexedSources = new Map()
const emptyIndex = {}
const months = []
const failures = new Set()
const kinds = {
    telegram: 'telegram_metadata.json',
    classification: 'classification_index.json',
    text: 'classification_text_index.json',
}
let loading = null
let complete = false
let query = null
let scanTimer = null

async function fetchJson(path) {
    if (!requests.has(path)) {
        const url = new URL(path, baseURL)
        url.searchParams.set('cb', cacheBust)
        requests.set(path, fetch(url).then(response => {
            if (!response.ok) throw new Error(`HTTP ${response.status} loading ${path}`)
            return response.json()
        }).then(value => {
            values.set(path, value)
            return value
        }).catch(error => {
            requests.delete(path)
            throw error
        }))
    }
    return requests.get(path)
}

function normalizeSearch(value) {
    return value.normalize('NFC').replace(/\s+/g, ' ').trim().toLowerCase()
        .replace(/[äöü]/g, char => ({ ä: 'a', ö: 'o', ü: 'u' })[char])
}

function compileSearch(query) {
    // Terms may span fields; quoted phrases must stay within one field or tag.
    const clauses = [...normalizeSearch(query).matchAll(/"([^"]*)(?:"|$)|([^\s"]+)/gu)]
        .map(match => (match[1] ?? match[2]).trim()).filter(Boolean)
    const escapeRegex = char => char.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const matchers = clauses.map(clause => {
        const punctuation = [...new Set(clause.match(/\p{P}/gu) || [])].map(escapeRegex).join('|')
        const ignored = punctuation ? `(?!(?:${punctuation}))\\p{P}` : '\\p{P}'
        const pattern = [...clause].map(char => char === ' '
            ? `(?:\\s|${ignored})+` : escapeRegex(char)).join(`(?:${ignored})*`)
        const regex = new RegExp(pattern, 'u')
        return text => text.includes(clause) || regex.test(text)
    })
    return fields => matchers.length > 0 && matchers.every(matches => fields.some(matches))
}

function classificationTags(classification) {
    if (!classification) return []
    if (!(classification.tag_format_version >= 2)) return classification.tags || []
    const seen = new Set()
    return ['tags', 'tags_de', 'tags_en'].flatMap(field => classification[field] || [])
        .filter(tag => {
            const key = tag.trim().toLowerCase()
            if (!key || seen.has(key)) return false
            seen.add(key)
            return true
        })
}

function indexMonth(month) {
    const root = `images/${month.dirName}/`
    const entries = values.get(root + 'entry_index.json')
    if (!entries) return
    const telegram = values.get(root + kinds.telegram) || emptyIndex
    const classifications = values.get(root + kinds.classification) || emptyIndex
    const text = values.get(root + kinds.text) || emptyIndex
    const sources = [entries, telegram, classifications, text]
    const previous = indexedSources.get(month.dirName)
    if (previous && sources.every((source, i) => source === previous[i])) return
    indexedSources.set(month.dirName, sources)
    for (let j = entries.length - 1; j >= 0; j--) {
        const entry = entries[j]
        const imageId = `${month.dirName}/${entry.name}`
        const classification = classifications[imageId]
        const details = text[imageId]
        records[month.start + entries.length - 1 - j] = {
            reactions: telegram[entry.name]?.[1] ?? 0,
            template: classification?.template || '',
            fields: [details?.text, details?.description, classification?.template,
                ...classificationTags(classification)].filter(Boolean).map(normalizeSearch),
        }
    }
}

function metadataStatus(kind) {
    if ([...failures].some(key => key.startsWith(`${kind}:`))) return 'unavailable'
    const indexedMonths = months.filter(month => values.has(`images/${month.dirName}/entry_index.json`))
    return indexedMonths.length && indexedMonths.every(month => values.has(`images/${month.dirName}/${kinds[kind]}`))
        ? 'ready' : 'loading'
}

function requiredMetadata(filters) {
    const required = []
    if (filters.minReactions > 0) required.push('telegram')
    if (filters.template || filters.search) required.push('classification')
    if (filters.search) required.push('text')
    return required
}

function scan() {
    scanTimer = null
    if (!query) return
    const { id, filters, matcher, required, indices } = query
    // Downloads finish independently, but only a settled newest-first prefix
    // may be published. Each query owns its boundary and matching indices.
    while (query.publishedMonths < months.length) {
        const month = months[query.publishedMonths]
        const root = `images/${month.dirName}/`
        const entries = values.get(root + 'entry_index.json')
        if (!entries && failures.has(`entries:${month.dirName}`)) {
            query.publishedMonths += 1
            continue
        }
        if (!entries || required.some(kind =>
            !values.has(root + kinds[kind]) && !failures.has(`${kind}:${month.dirName}`))) break
        // Failed required chunks are settled too. Freeze each published month's
        // matches so an incidental browsing retry cannot insert newer results
        // into this query; a new query will pick up any recovered metadata.
        indexMonth(month)
        for (let i = month.start; i < month.start + entries.length; i++) {
            const item = records[i]
            if (!item || item.reactions < filters.minReactions ||
                (filters.template && item.template !== filters.template) ||
                (filters.search && !matcher(item.fields))) continue
            indices.push(i)
        }
        query.publishedMonths += 1
    }
    self.postMessage({ type: 'results', id, indices, complete, error: failures.size > 0,
        statuses: Object.fromEntries(Object.keys(kinds).map(kind => [kind, metadataStatus(kind)])) })
}

function scheduleScan() {
    // Coalesce month/metadata completions, rather than rescanning for every response.
    if (scanTimer === null) scanTimer = setTimeout(scan, 30)
}

async function loadArchive(dirIndex) {
    if (loading) return loading
    if (!months.length) {
        let start = 0
        for (const dirName of Object.keys(dirIndex).sort().reverse()) {
            if (dirIndex[dirName] > 0) months.push({ dirName, start })
            start += dirIndex[dirName]
        }
    }
    complete = false
    // A retry makes previous failures pending again before any query can
    // publish older matches based on the previous attempt's settled state.
    failures.clear()
    loading = (async () => {
        let cursor = 0
        await Promise.all(Array.from({ length: Math.min(6, months.length) }, async () => {
            while (cursor < months.length) {
                const month = months[cursor++]
                const root = `images/${month.dirName}/`
                const entryKey = `entries:${month.dirName}`
                try {
                    const entries = await fetchJson(root + 'entry_index.json')
                    failures.delete(entryKey)
                    self.postMessage({ type: 'month', ...month, entries })
                    indexMonth(month)
                    scheduleScan()
                } catch (error) {
                    failures.add(entryKey)
                    scheduleScan()
                    continue
                }
                await Promise.all(Object.entries(kinds).map(async ([kind, filename]) => {
                    const key = `${kind}:${month.dirName}`
                    try {
                        const data = await fetchJson(root + filename)
                        failures.delete(key)
                        self.postMessage({ type: 'metadata', dirName: month.dirName, kind, data })
                    } catch (error) {
                        failures.add(key)
                        self.postMessage({ type: 'metadata', dirName: month.dirName, kind, error: String(error) })
                    }
                    indexMonth(month)
                    scheduleScan()
                }))
            }
        }))
        complete = true
        clearTimeout(scanTimer)
        scan()
        self.postMessage({ type: 'loaded', error: failures.size > 0 })
        loading = null
    })()
    return loading
}

self.onmessage = async ({ data }) => {
    try {
        if (data.type === 'init') {
            baseURL = data.baseURL
            cacheBust = data.cacheBust
        } else if (data.type === 'fetch') {
            try {
                const value = await fetchJson(data.path)
                self.postMessage({ type: 'fetched', id: data.id, value })
            } catch (error) {
                self.postMessage({ type: 'fetched', id: data.id, error: String(error) })
            }
        } else if (data.type === 'load') {
            await loadArchive(data.dirIndex)
        } else if (data.type === 'cancel') {
            query = null
            clearTimeout(scanTimer)
            scanTimer = null
        } else if (data.type === 'query') {
            query = { id: data.id, filters: data.filters, matcher: compileSearch(data.filters.search),
                required: requiredMetadata(data.filters), publishedMonths: 0, indices: [] }
            clearTimeout(scanTimer)
            scan()
        }
    } catch (error) {
        self.postMessage({ type: 'fatal', error: String(error) })
    }
}
