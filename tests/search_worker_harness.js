const { readFileSync } = require('node:fs')
const vm = require('node:vm')

const workerSource = readFileSync(`${__dirname}/../assets/search-worker.js`, 'utf8')

// Exercise the actual worker protocol with structured-cloned, asynchronous messages.
function workerClass(fetchJson, options = {}) {
    return class Worker {
        constructor(url) {
            if (options.unavailable) throw new Error('Workers disabled')
            this.url = url
            this.sent = []
            this.received = []
            this.terminated = false
            let timerId = 0
            const timers = new Set()
            this.context = {
                URL,
                fetch: async url => {
                    const path = new URL(url).pathname.replace(/^\//, '')
                    const value = await fetchJson(path)
                    return { ok: true, json: async () => structuredClone(value) }
                },
                setTimeout(callback) {
                    const id = ++timerId
                    timers.add(id)
                    queueMicrotask(() => { if (timers.delete(id)) callback() })
                    return id
                },
                clearTimeout(id) { timers.delete(id) },
                self: { postMessage: data => {
                    const clone = structuredClone(data)
                    this.received.push(clone)
                    const deliver = () => {
                        if (!this.terminated) this.onmessage?.({ data: clone })
                    }
                    if (options.deliver) options.deliver(clone, deliver)
                    else queueMicrotask(deliver)
                } },
            }
            vm.runInNewContext(workerSource, this.context)
            options.instances?.push(this)
        }
        postMessage(data) {
            const clone = structuredClone(data)
            this.sent.push(clone)
            queueMicrotask(() => {
                if (!this.terminated) this.context.self.onmessage({ data: clone })
            })
        }
        terminate() { this.terminated = true }
    }
}

module.exports = { workerClass, workerSource }
