const assert = require('node:assert/strict')
const { createServer } = require('node:http')
const { Worker } = require('node:worker_threads')
const { test } = require('node:test')
const { workerClass, workerSource } = require('./search_worker_harness')

function messages(worker) {
    const received = []
    const waiters = new Set()
    worker.on('message', data => {
        received.push(data)
        for (const waiter of waiters) {
            if (waiter.predicate(data)) {
                waiters.delete(waiter)
                clearTimeout(waiter.timer)
                waiter.resolve(data)
            }
        }
    })
    return predicate => {
        const existing = received.find(predicate)
        if (existing) return Promise.resolve(existing)
        return new Promise((resolve, reject) => {
            const waiter = { predicate, resolve }
            waiter.timer = setTimeout(() => {
                waiters.delete(waiter)
                reject(new Error('Timed out waiting for worker message'))
            }, 5000)
            waiters.add(waiter)
        })
    }
}

test('real worker fetches and filters monthly metadata, reuses HTTP cache, and retries failures', async t => {
    const paths = []
    let failText = true
    const months = ['2024/01', '2024/02']
    const fixtures = {}
    for (const month of months) {
        const filename = `${month.replace('/', '-')}-01_a.jpg`
        const imageId = `${month}/${filename}`
        fixtures[`images/${month}/entry_index.json`] = [{ name: filename, w: 100, h: 100 }]
        fixtures[`images/${month}/telegram_metadata.json`] = { [filename]: [1, month.endsWith('02') ? 7 : 2] }
        fixtures[`images/${month}/classification_index.json`] = {
            [imageId]: { template: 'Example', tag_format_version: 2, tags: ['shared'], tags_de: ['Äpfel'], tags_en: ['taxation'] },
        }
        fixtures[`images/${month}/classification_text_index.json`] = {
            [imageId]: { text: 'Steuern sind Diebstahl', description: 'Hello_world!' },
        }
    }
    const server = createServer((req, res) => {
        const url = new URL(req.url, 'http://localhost')
        assert.equal(url.searchParams.get('cb'), 'test-cache')
        const path = url.pathname.slice(1)
        paths.push(path)
        if (failText && path === 'images/2024/01/classification_text_index.json') {
            res.writeHead(503).end('offline')
        } else {
            res.setHeader('Content-Type', 'application/json')
            res.end(JSON.stringify(fixtures[path]))
        }
    })
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    t.after(() => new Promise(resolve => server.close(resolve)))
    const worker = new Worker(`
        const { parentPort, isMainThread } = require('node:worker_threads');
        if (isMainThread) throw new Error('Search must run off the main thread');
        global.self = { postMessage: data => parentPort.postMessage(data) };
        parentPort.on('message', data => self.onmessage({ data }));
        ${workerSource}
    `, { eval: true })
    t.after(() => worker.terminate())
    const wait = messages(worker)
    worker.postMessage({ type: 'init', baseURL: `http://127.0.0.1:${server.address().port}/?q=ignored`, cacheBust: 'test-cache' })
    // Browsing and filtering must not download/parse a month twice.
    worker.postMessage({ type: 'fetch', id: 100, path: 'images/2024/02/entry_index.json' })
    assert.equal((await wait(data => data.type === 'fetched' && data.id === 100)).value.length, 1)
    worker.postMessage({ type: 'load', dirIndex: Object.fromEntries(months.map(month => [month, 1])) })
    worker.postMessage({ type: 'query', id: 1, filters: { minReactions: 7, template: 'Example', search: '"steuern sind diebstahl" apfel taxation' } })
    const result = await wait(data => data.type === 'results' && data.id === 1 && data.complete)
    assert.deepEqual(result.indices, [0])
    assert.equal(result.error, true)
    assert.equal(result.statuses.text, 'unavailable')
    await wait(data => data.type === 'loaded')
    for (const path of Object.keys(fixtures)) assert.equal(paths.filter(value => value === path).length, 1, path)

    // Queries alone reuse already-normalized data without additional network requests.
    worker.postMessage({ type: 'query', id: 2, filters: { minReactions: 0, template: '', search: 'apfel shared' } })
    assert.deepEqual((await wait(data => data.type === 'results' && data.id === 2)).indices, [0, 1])
    assert.equal(paths.length, Object.keys(fixtures).length)
    worker.postMessage({ type: 'query', id: 3, filters: { minReactions: 0, template: '', search: '.*' } })
    assert.deepEqual((await wait(data => data.type === 'results' && data.id === 3)).indices, [])

    failText = false
    worker.postMessage({ type: 'load', dirIndex: Object.fromEntries(months.map(month => [month, 1])) })
    worker.postMessage({ type: 'query', id: 4, filters: { minReactions: 0, template: '', search: 'hello world' } })
    const retried = await wait(data => data.type === 'results' && data.id === 4 && data.complete && !data.error)
    assert.deepEqual(retried.indices, [0, 1])
    assert.equal(retried.statuses.text, 'ready')
    assert.equal(paths.length, Object.keys(fixtures).length + 1, 'only the failed chunk is fetched again')
})

const flush = () => new Promise(resolve => setImmediate(resolve))
const filters = (changes = {}) => ({ minReactions: 0, template: '', search: 'match', ...changes })

function orderedArchive(heldPaths = []) {
    const dirs = ['2024/04', '2024/03', '2024/02', '2024/01']
    const fixtures = {}
    for (const dir of dirs) {
        const filename = `${dir.replace('/', '-')}-01_a.jpg`
        const imageId = `${dir}/${filename}`
        const root = `images/${dir}/`
        fixtures[root + 'entry_index.json'] = [{ name: filename, w: 100, h: 100 }]
        fixtures[root + 'telegram_metadata.json'] = { [filename]: [1, 7] }
        fixtures[root + 'classification_index.json'] = { [imageId]: { tags: ['tag'], template: 'Example' } }
        fixtures[root + 'classification_text_index.json'] = { [imageId]: { text: 'match', description: '' } }
    }
    const pending = new Map()
    const held = new Set(heldPaths)
    const requested = []
    const Worker = workerClass(path => {
        requested.push(path)
        if (held.has(path)) return new Promise((resolve, reject) => { pending.set(path, { resolve, reject }) })
        return fixtures[path]
    })
    const worker = new Worker('/assets/search-worker.js')
    worker.postMessage({ type: 'init', baseURL: 'https://gallery.example/', cacheBust: 'test' })
    const dirIndex = Object.fromEntries(dirs.map(dir => [dir, 1]))
    return {
        worker, pending, requested,
        load() { worker.postMessage({ type: 'load', dirIndex }) },
        query(id, changes) { worker.postMessage({ type: 'query', id, filters: filters(changes) }) },
        async settle(path, fail = false) {
            const response = pending.get(path)
            assert.ok(response, `Request pending: ${path}`)
            pending.delete(path)
            held.delete(path)
            if (fail) response.reject(new Error('offline'))
            else response.resolve(fixtures[path])
            await flush()
        },
        results(id) { return worker.received.filter(data => data.type === 'results' && data.id === id) },
        latest(id) { return this.results(id).at(-1) },
    }
}

test('out-of-order search completions publish only settled chronological prefixes and append matches', async () => {
    const classification = 'images/2024/04/classification_index.json'
    const newestText = 'images/2024/04/classification_text_index.json'
    const middleText = 'images/2024/02/classification_text_index.json'
    const archive = orderedArchive([classification, newestText, middleText])
    archive.load()
    archive.query(1)
    await flush()
    assert.deepEqual(archive.latest(1).indices, [], 'ready older matches stay buffered')
    assert.equal(archive.latest(1).complete, false)
    assert.ok(archive.requested.includes('images/2024/01/classification_text_index.json'), 'older requests still run in parallel')
    await archive.settle(newestText)
    assert.deepEqual(archive.latest(1).indices, [], 'OCR alone cannot publish before tags/templates settle')
    await archive.settle(classification)
    assert.deepEqual(archive.latest(1).indices, [0, 1], 'release consecutive completed newer months together')
    assert.equal(archive.latest(1).complete, false)
    await archive.settle(middleText)
    assert.deepEqual(archive.latest(1).indices, [0, 1, 2, 3])
    assert.equal(archive.latest(1).complete, true)
    let previous = []
    for (const result of archive.results(1)) {
        assert.deepEqual(result.indices.slice(0, previous.length), previous, 'published matches never shift')
        previous = result.indices
    }
})

test('publication requirements change with the query and ignore unrelated pending chunks', async () => {
    const text = 'images/2024/04/classification_text_index.json'
    const reactions = 'images/2024/03/telegram_metadata.json'
    const archive = orderedArchive([text, reactions])
    archive.load()
    archive.query(1)
    await flush()
    assert.deepEqual(archive.latest(1).indices, [])
    archive.query(2, { search: '', minReactions: 7 })
    await flush()
    assert.deepEqual(archive.latest(2).indices, [0], 'reaction filter waits for newer reactions, not OCR')
    archive.query(3, { search: '', template: 'Example' })
    await flush()
    assert.deepEqual(archive.latest(3).indices, [0, 1, 2, 3], 'template filter needs only classifications')
    archive.query(4)
    await flush()
    assert.deepEqual(archive.latest(4).indices, [], 'search cannot inherit the template boundary')
    await archive.settle(text)
    assert.deepEqual(archive.latest(4).indices, [0, 1, 2, 3], 'unrelated reaction request does not delay search')
    archive.query(5, { minReactions: 7 })
    await flush()
    assert.deepEqual(archive.latest(5).indices, [0], 'combined filters require both kinds of metadata')
    await archive.settle(reactions)
    assert.deepEqual(archive.latest(5).indices, [0, 1, 2, 3])
    assert.ok(!archive.worker.received.some(data => data.type === 'results' && data.id === 1 && data.indices.length),
        'completions must not revive a superseded query')
})

test('a browsing retry cannot insert matches into an already published month of the current query', async () => {
    const newest = 'images/2024/04/classification_text_index.json'
    const older = 'images/2024/02/telegram_metadata.json'
    const archive = orderedArchive([newest, older])
    archive.load()
    archive.query(1)
    await flush()
    await archive.settle(newest, true)
    assert.deepEqual(archive.latest(1).indices, [1, 2, 3])
    archive.worker.postMessage({ type: 'fetch', id: 100, path: newest })
    await flush()
    await archive.settle(older)
    assert.deepEqual(archive.latest(1).indices, [1, 2, 3], 'recovering a failed chunk must not shift existing results')
    archive.query(2)
    await flush()
    assert.deepEqual(archive.latest(2).indices, [0, 1, 2, 3], 'a new query picks up the recovered metadata')
})

for (const filename of ['entry_index.json', 'classification_index.json', 'classification_text_index.json']) {
    test(`a failed newest ${filename} releases older results, and retry restores the boundary`, async () => {
        const path = `images/2024/04/${filename}`
        const older = 'images/2024/02/telegram_metadata.json'
        const archive = orderedArchive([path, older])
        archive.load()
        archive.query(1)
        await flush()
        assert.deepEqual(archive.latest(1).indices, [])
        await archive.settle(path, true)
        const expected = filename === 'classification_index.json' ? [0, 1, 2, 3] : [1, 2, 3]
        assert.deepEqual(archive.latest(1).indices, expected, 'failed months must not hold older matches indefinitely')
        assert.equal(archive.latest(1).error, true)
        assert.equal(archive.latest(1).complete, false, 'other archive requests are still pending')
        await archive.settle(older)
        assert.equal(archive.latest(1).complete, true)

        // Delay the retry too: previous failure must become pending immediately.
        const retry = new Promise(resolve => {
            const fetch = archive.worker.context.fetch
            archive.worker.context.fetch = url => {
                if (new URL(url).pathname === '/' + path) return new Promise(done => { resolve(() => done(fetch(url))) })
                return fetch(url)
            }
        })
        archive.load()
        archive.query(2)
        await flush()
        assert.deepEqual(archive.latest(2).indices, [], 'retry may find a newer match, so older matches are buffered again')
        const release = await retry
        release()
        await flush()
        assert.deepEqual(archive.latest(2).indices, [0, 1, 2, 3])
        assert.equal(archive.latest(2).error, false)
        assert.equal(archive.latest(2).complete, true)
    })
}
