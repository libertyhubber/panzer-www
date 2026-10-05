const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { test } = require('node:test')
const vm = require('node:vm')

const source = readFileSync(`${__dirname}/../assets/navigation.js`, 'utf8')
const methods = ['paypal', 'lightning', 'bitcoin', 'monero']
const addresses = {
    paypal: 'https://paypal.me/rosarotepanzer',
    lightning: 'LNURL1DP68GURN8GHJ7AMPD3KX2AR0VEEKZAR0WD5XJTNRDAKJ7TNHV4KXCTTTDEHHWM30D3H82UNVWQHKZ6TJWD5KX6MRV9KHQVFJV3AQXK',
    bitcoin: 'bc1qtavlxx50eu8djwmlajj6dsch4nrmpmzgrawg5l',
    monero: '45oesfHwHm3TKcKNosJXxE2J2hBh3GajNU5BcaozvacyiBwsNgygdKqBfVQeD8SmsKNhpe6hSLvbdF1vsDjnJ83kELKBCgJ',
}

function navigation(clipboard = { writeText: async () => {} }) {
    let activeElement
    function element(dataset = {}) {
        const listeners = {}
        const attrs = {}
        return {
            dataset, disabled: true,
            setAttribute(name, value) { attrs[name] = value },
            getAttribute(name) { return attrs[name] },
            focus() { activeElement = this },
            addEventListener(name, handler) { (listeners[name] ||= []).push(handler) },
            emit(name, event = {}) { return Promise.all((listeners[name] || []).map(handler => handler(event))) },
        }
    }
    const buttons = methods.map(id => element({ navMethod: id }))
    const panels = methods.map(id => {
        const panel = element({ navPanel: id })
        panel.copy = element()
        panel.address = element()
        panel.address.value = addresses[id]
        panel.address.select = function () { this.selected = true }
        panel.status = { textContent: '' }
        panel.querySelector = selector => ({
            '[data-nav-copy]': panel.copy, 'textarea': panel.address,
            '.site-nav-copy-status': panel.status,
        })[selector]
        return panel
    })
    const dialogs = ['creator-links', 'creator-support'].map(id => {
        const dialog = element()
        dialog.id = id
        dialog.closeButton = element()
        dialog.querySelector = () => dialog.closeButton
        dialog.querySelectorAll = selector => selector === '[data-nav-method]' ? buttons : panels
        dialog.getBoundingClientRect = () => ({ left: 100, right: 500, top: 100, bottom: 700 })
        dialog.showModal = function () { this.open = true; this.closeButton.focus() }
        dialog.close = function () { this.open = false; this.emit('close') }
        return dialog
    })
    const triggers = dialogs.map(dialog => element({ navOpen: dialog.id }))
    const classes = new Set()
    const document = {
        body: { classList: { add: name => classes.add(name), remove: name => classes.delete(name) } },
        getElementById: id => dialogs.find(dialog => dialog.id === id),
        querySelectorAll: () => triggers,
    }
    vm.runInNewContext(source, { document, navigator: { clipboard } })
    return { buttons, panels, dialogs, triggers, classes, get activeElement() { return activeElement } }
}

test('navigation initializes independently of the gallery and selects PayPal', () => {
    const ui = navigation()
    assert.ok(ui.buttons.every(button => !button.disabled))
    assert.ok(ui.triggers.every(button => !button.disabled))
    assert.ok(ui.panels.every(panel => !panel.copy.disabled))
    assert.deepEqual(ui.buttons.map(button => button.getAttribute('aria-pressed')), ['true', 'false', 'false', 'false'])
    assert.deepEqual(ui.panels.map(panel => panel.hidden), [false, true, true, true])
    vm.runInNewContext(source, { document: { getElementById: () => null } })
})

test('method selection updates the visible panel and clears copy status', async () => {
    const ui = navigation()
    ui.panels[0].status.textContent = 'Kopiert.'
    await ui.buttons[2].emit('click')
    assert.deepEqual(ui.buttons.map(button => button.getAttribute('aria-pressed')), ['false', 'false', 'true', 'false'])
    assert.deepEqual(ui.panels.map(panel => panel.hidden), [true, true, false, true])
    assert.ok(ui.panels.every(panel => panel.status.textContent === ''))
})

test('each copy button copies the current payment target', async () => {
    const copied = []
    const ui = navigation({ writeText: async text => { copied.push(text) } })
    for (let index = 0; index < methods.length; index += 1) {
        await ui.buttons[index].emit('click')
        await ui.panels[index].copy.emit('click')
        assert.equal(ui.panels[index].status.textContent, 'Kopiert.')
    }
    assert.deepEqual(copied, methods.map(id => addresses[id]))
})

test('clipboard fallback selects the address when the API is unavailable or rejects', async () => {
    for (const clipboard of [null, { writeText: async () => { throw Error('Denied') } }]) {
        const ui = navigation(clipboard)
        await ui.panels[0].copy.emit('click')
        assert.equal(ui.activeElement, ui.panels[0].address)
        assert.equal(ui.panels[0].address.selected, true)
        assert.match(ui.panels[0].status.textContent, /manuell kopieren/)
    }
})

test('a delayed copy cannot update status or focus after a method change', async () => {
    let reject
    const ui = navigation({ writeText: () => new Promise((_, fail) => { reject = fail }) })
    const copy = ui.panels[0].copy.emit('click')
    await ui.buttons[3].emit('click')
    reject(Error('Denied'))
    await copy
    assert.equal(ui.panels[0].status.textContent, '')
    assert.equal(ui.activeElement, undefined)
})

test('dialogs restore trigger focus, expanded state, and scroll after close', async () => {
    const ui = navigation()
    for (let index = 0; index < ui.dialogs.length; index += 1) {
        await ui.triggers[index].emit('click')
        assert.equal(ui.dialogs[index].open, true)
        assert.equal(ui.triggers[index].getAttribute('aria-expanded'), 'true')
        assert.ok(ui.classes.has('site-nav-modal-open'))
        await ui.dialogs[index].closeButton.emit('click')
        assert.equal(ui.dialogs[index].open, false)
        assert.equal(ui.triggers[index].getAttribute('aria-expanded'), 'false')
        assert.equal(ui.activeElement, ui.triggers[index])
        assert.equal(ui.classes.has('site-nav-modal-open'), false)
    }
})

test('backdrop dismissal requires both press and release outside the dialog', async () => {
    const ui = navigation()
    const dialog = ui.dialogs[1]
    const inside = { clientX: 200, clientY: 200 }
    const outside = { clientX: 20, clientY: 20 }
    await ui.triggers[1].emit('click')
    for (const [press, release] of [[inside, outside], [outside, inside]]) {
        await dialog.emit('pointerdown', press)
        await dialog.emit('click', release)
        assert.equal(dialog.open, true)
    }
    await dialog.emit('pointerdown', outside)
    await dialog.emit('click', outside)
    assert.equal(dialog.open, false)
})

test('closing a dialog invalidates pending clipboard work', async () => {
    let resolve
    const ui = navigation({ writeText: () => new Promise(done => { resolve = done }) })
    await ui.triggers[1].emit('click')
    const copy = ui.panels[0].copy.emit('click')
    ui.dialogs[1].close()
    resolve()
    await copy
    assert.equal(ui.panels[0].status.textContent, '')
})

test('page reserves scrollbar space while navigation dialogs lock scroll', () => {
    const css = readFileSync(`${__dirname}/../assets/style.css`, 'utf8')
    const navigationCss = readFileSync(`${__dirname}/../assets/navigation.css`, 'utf8')
    assert.match(css, /html\s*\{[^}]*scrollbar-gutter:\s*stable;/)
    assert.match(css, /body\s*\{[^}]*overflow-y:\s*scroll;/)
    assert.match(navigationCss, /body\.site-nav-modal-open\s*\{\s*overflow:\s*hidden;/)
})

test('generated main page includes approved navigation and retains the gallery', () => {
    const html = readFileSync(`${__dirname}/../index.html`, 'utf8')
    assert.match(html, /Inoffizielles Meme-Archiv/)
    assert.match(html, /Panzer Unterstützen/)
    assert.doesNotMatch(html, /top-nav|>MEMES<|tmp\/nav-experiments|Den Creator unterstützen|built-in method/)
    assert.match(html, /\/assets\/navigation\.js\?cb=1/)
    assert.match(html, /id="gallery-filters"/)
    assert.match(html, /id="gallery"/)
    assert.match(html, /PhotoSwipeLightbox/)
    assert.equal([...html.matchAll(/class="site-nav-social"/g)].length, 6)
    const links = html.match(/<nav[^>]*aria-label="Creator-Links">([\s\S]*?)<\/nav>/)[1]
    assert.match(links, /^\s*<a class="site-nav-social site-nav-featured" href="https:\/\/lib-lib\.org\/" target="_blank" rel="noopener noreferrer">/)
    assert.match(links, /<strong>Freie Übersetzungen libertärer Werke<\/strong>/)
    assert.match(html, /\/assets\/navigation\.css\?cb=2/)
    assert.match(html, /href="https:\/\/instagram\.com\/rosarotepanzer2"/)
    assert.match(html, /<strong>Instagram<\/strong><small>@rosarotepanzer2<\/small>/)
    assert.deepEqual([...html.matchAll(/data-nav-method="([^"]+)"/g)].map(match => match[1]), methods)
    assert.deepEqual([...html.matchAll(/data-nav-copy disabled>([^<]+)</g)].map(match => match[1]),
        ['Link kopieren', 'LNURL kopieren', 'Adresse kopieren', 'Adresse kopieren'])
    for (const id of methods) {
        assert.match(html, new RegExp(`class="site-nav-qr icon ${id}"`))
        assert.ok(html.includes(addresses[id]))
    }
    assert.match(html, /href="https:\/\/www\.gofundme\.com\/f\/wir-mussen-mehr-rothbard-lesen" target="_blank" rel="noopener noreferrer"/)
    const css = readFileSync(`${__dirname}/../assets/navigation.css`, 'utf8')
    assert.match(css, /\.site-nav-donation\[hidden\]/)
    assert.doesNotMatch(css, /background-image:/, 'QR and SVG images must come from the original icons.css')
})
