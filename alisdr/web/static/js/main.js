import { api, errorToast, h, icon, loadLanguage, mount, t, toast } from './core.js';
import { liveAudio } from './live-audio.js';
import { S, addonsInUse, emit, loadSettings, maybeShowLaunchNotice, on, startPolling } from './state.js';
import { channelsView } from './views/channels.js';
import { listenView } from './views/listen.js';
import { manualView } from './views/manual.js';
import { messagesView } from './views/messages.js';
import { recordingsView } from './views/recordings.js';
import { scheduleView } from './views/schedule.js';
import { settingsView } from './views/settings.js';
import { statsView } from './views/stats.js';

const app = document.getElementById('app');

function receiverView(root, hash) {
  const active = hash === '#/listen/scan' ? 'scan' : 'tune';
  const modes = h('div.seg.tuner-mode', { role: 'group', 'aria-label': t('Receiver mode') },
    ...[['tune', t('Tune')], ['scan', t('Scan')]].map(([value, label]) => h('button', {
      type: 'button', class: active === value ? 'active' : '', 'aria-pressed': String(active === value),
      onclick: () => { if (active !== value) location.hash = `#/listen/${value}`; },
    }, label)));
  return active === 'scan' ? listenView(root, modes) : manualView(root, modes);
}

const ROUTES = {
  listen: { label: () => t('Listen'), icon: 'listen', view: receiverView },
  recordings: { label: () => t('Recordings'), icon: 'library', view: recordingsView },
  // What the add-on decoders found. Shown once add-ons are in use (Add-ons is the always-visible showcase); with
  // nothing received yet it explains itself and points to Add-ons.
  messages: { label: () => t('Messages'), icon: 'messages', view: messagesView, show: addonsInUse },
  channels: { label: () => t('Channels'), icon: 'channels', view: channelsView },
  schedule: { label: () => t('Schedule'), icon: 'clock', view: scheduleView },
  stats: { label: () => t('Statistics'), icon: 'stats', view: statsView },
  // Decoders and tools, plus transcription and digital voice (the same page component as Settings).
  addons: { label: () => t('Add-ons'), icon: 'addons', view: (root, hash) => settingsView(root, hash, 'addons') },
  settings: { label: () => t('Settings'), icon: 'settings', view: settingsView },
};

async function boot() {
  try {
    await loadLanguage();
  } catch {
    mount(app, h('div.center-page', h('div.card.login', h('h2', 'AliSDR'), h('p.muted', 'Server unreachable - retrying…'))));
    setTimeout(boot, 3000);
    return;
  }
  const session = await api.get('/api/session');
  if (!session.authenticated) return loginPage();
  await loadSettings();
  shell();
  startPolling();
  maybeShowLaunchNotice();
}

function loginPage() {
  const input = h('input', { type: 'password', placeholder: t('Password'), autocomplete: 'current-password' });
  const submit = async (e) => {
    e.preventDefault();
    try {
      await api.post('/api/login', { token: input.value });
      location.reload();
    } catch { toast(t('Wrong password'), 'bad'); input.select(); }
  };
  mount(app, h('div.center-page', h('form.card.login', { onsubmit: submit },
    h('img', { src: 'icon.svg', alt: '' }),
    h('h2', 'AliSDR'),
    h('p.muted', t('Enter the password of this receiver.')),
    input,
    h('button.primary.big', { type: 'submit' }, t('Log in')),
    h('p.faint.small', t('The password was set in Settings → Access. To remove it, delete "access_token" from config.json on the receiver.')))));
  input.focus();
}

let current = null;

function shell() {
  const defaultRoute = 'listen';
  const routeKey = () => {
    const key = (location.hash.match(/^#\/(\w+)/) || [])[1];
    return key && key !== 'manual' ? key : defaultRoute;
  };
  const nav = h('nav.nav');
  const status = h('div.status');
  const content = h('main');
  mount(app, h('div.shell',
    h('header.topbar',
      h('a.brand', { href: '#/' + defaultRoute }, h('img', { src: 'icon.svg', alt: '' }), 'AliSDR', h('small', t('server'))),
      nav, status),
    content));
  const markActive = () => {
    const key = routeKey();
    nav.querySelectorAll('a').forEach((a) => {
      a.classList.toggle('active', a.dataset.route === key);
      if (a.dataset.route === key) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
  };
  const renderNav = () => {
    nav.replaceChildren(...Object.entries(ROUTES).filter(([, r]) => !r.show || r.show())
      .map(([key, r]) => h('a', { href: `#/${key}`, 'data-route': key }, icon(r.icon), h('span', r.label()))));
    markActive();
  };
  renderNav();
  on('settings', renderNav);
  const updateStatus = () => {
    const live = S.live;
    const running = live && live.running;
    const signal = live && (live.state === 'SIGNAL_DETECTED' || live.state === 'LINGER');
    mount(status,
      live && (live.recording || live.manual_tuning_recording) ? h('span.chip.bad.live', { title: t('Recording') }, h('span.dot'), 'REC') : null,
      live && live.manual_tuning
        ? h('span.chip.good.live', h('span.dot'), t('Tuning'))
        : live && live.addon
        ? h('span.chip.good.live', h('span.dot'), t('Add-on running'))
        : h('span.chip', { class: running ? (signal ? 'good live' : 'accent') : '' }, h('span.dot'),
          running ? (live.stopping ? t('Stopping…') : signal ? t('Signal') : t('Scanning')) : t('Stopped')),
      h('button.icon', {
        class: liveAudio.playing ? 'on' : '', title: liveAudio.playing ? t('Stop listening') : t('Listen live'),
        onclick: () => liveAudio.toggle().catch(errorToast),
      }, icon(liveAudio.playing ? 'headphones' : 'mute')));
  };
  on('live', (live) => {
    liveAudio.useBroadcast(Boolean(live.manual_broadcast ?? live.manual_dab));
    updateStatus();
  });
  // Notifications: a notice in the app whenever a rule fires (the first poll only sets the starting point).
  let notifySeq = null;
  on('live', async (live) => {
    if (live.notify_seq === undefined) return;
    if (notifySeq === null) { notifySeq = live.notify_seq; return; }
    if (live.notify_seq <= notifySeq) return;
    const since = notifySeq;
    notifySeq = live.notify_seq;
    try { (await api.get(`/api/notify/recent?since=${since}`)).items.slice(-3).forEach((i) => toast(`${i.title}: ${i.message}`)); } catch { /* the list shows it */ }
  });
  liveAudio.onchange = () => { updateStatus(); emit('audio'); };
  updateStatus();

  const route = () => {
    const key = routeKey();
    const r = ROUTES[key] || ROUTES[defaultRoute];
    markActive();
    if (current && current.destroy) current.destroy();
    content.scrollTop = 0;
    current = r.view(content, location.hash);
  };
  window.addEventListener('hashchange', route);
  route();
}

boot().catch((e) => { console.error(e); errorToast(e); });
