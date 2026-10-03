(() => {
    'use strict'

    const support = document.getElementById('creator-support')
    if (!support) return

    const methods = [...support.querySelectorAll('[data-nav-method]')]
    const panels = [...support.querySelectorAll('[data-nav-panel]')]
    let copyVersion = 0

    function selectMethod(id) {
        copyVersion += 1
        for (const button of methods) {
            button.setAttribute('aria-pressed', String(button.dataset.navMethod === id))
        }
        for (const panel of panels) {
            panel.hidden = panel.dataset.navPanel !== id
            panel.querySelector('.site-nav-copy-status').textContent = ''
        }
    }

    for (const button of methods) {
        button.addEventListener('click', () => selectMethod(button.dataset.navMethod))
        button.disabled = false
    }
    selectMethod(methods[0].dataset.navMethod)

    for (const panel of panels) {
        const copy = panel.querySelector('[data-nav-copy]')
        const address = panel.querySelector('textarea')
        const status = panel.querySelector('.site-nav-copy-status')
        copy.disabled = false
        copy.addEventListener('click', async () => {
            const version = ++copyVersion
            try {
                await navigator.clipboard.writeText(address.value)
                if (version === copyVersion) status.textContent = 'Kopiert.'
            } catch {
                if (version !== copyVersion) return
                address.focus()
                address.select()
                status.textContent = 'Bitte die markierte Adresse manuell kopieren.'
            }
        })
    }

    for (const trigger of document.querySelectorAll('[data-nav-open]')) {
        const dialog = document.getElementById(trigger.dataset.navOpen)
        trigger.addEventListener('click', () => {
            dialog.showModal()
            trigger.setAttribute('aria-expanded', 'true')
            document.body.classList.add('site-nav-modal-open')
        })
        trigger.disabled = false
        dialog.querySelector('[data-nav-close]').addEventListener('click', () => dialog.close())

        // Close only if both press and release occur on the backdrop.
        let pressedOutside = false
        function outside(event) {
            const rect = dialog.getBoundingClientRect()
            return event.clientX < rect.left || event.clientX > rect.right ||
                event.clientY < rect.top || event.clientY > rect.bottom
        }
        dialog.addEventListener('pointerdown', event => { pressedOutside = outside(event) })
        dialog.addEventListener('click', event => {
            if (pressedOutside && outside(event)) dialog.close()
            pressedOutside = false
        })
        dialog.addEventListener('close', () => {
            copyVersion += 1
            pressedOutside = false
            trigger.setAttribute('aria-expanded', 'false')
            document.body.classList.remove('site-nav-modal-open')
            trigger.focus()
        })
    }
})()
