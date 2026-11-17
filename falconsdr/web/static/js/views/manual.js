import { api, errorToast, h, mount, t } from '../core.js';
import { liveAudio } from '../live-audio.js';
import { on } from '../state.js';

const analogModes = ['AM', 'NFM', 'WFM', 'SSB', 'USB', 'LSB'];

export function manualView(root) {
  const receiver = h('select', { required: true });
  const mode = h('select', {}, ...[...analogModes, 'DAB'].map((value) =>
    h('option', { value }, value === 'DAB' ? 'DAB/DAB+' : value)));
  const frequency = h('input', {
    type: 'number', inputmode: 'decimal', min: '0.000001', max: '6000', step: '0.000001',
    value: '121.5', required: true,
  });
  const channel = h('select');
  const frequencyField = h('label.field', t('Frequency (MHz)'), frequency);
  const channelField = h('label.field', t('DAB channel'), channel);
  const tune = h('button.primary', { type: 'submit', disabled: true }, t('Tune'));
  const stop = h('button.ghost', { type: 'button', disabled: true }, t('Stop tuning'));
  const listen = h('button.ghost', { type: 'button', hidden: true, onclick: () => liveAudio.toggle().catch(errorToast) });
  const status = h('p.muted', { role: 'status', 'aria-live': 'polite' }, t('Loading…'));
  const stationList = h('select');
  const audio = h('audio', { controls: true, preload: 'none', 'aria-label': t('DAB audio') });
  const play = h('button.primary', { type: 'button' }, t('Play station'));
  const playbackError = h('p.muted', { role: 'status', hidden: true },
    t('This DAB station is unavailable. Try playing it again.'));
  const stations = h('div.col', { hidden: true },
    h('label.field', t('DAB station'), stationList), h('div.row.wrap', play, audio), playbackError);

  let destroyed = false;
  let busy = false;
  let ready = false;
  let active = null;
  let activeKey = '';
  let stationKey = '';
  let receiverKey = '';
  let revision = 0;
  let timer;
  const updateMode = () => {
    const dab = mode.value === 'DAB';
    frequencyField.hidden = frequency.disabled = dab;
    channelField.hidden = channel.disabled = !dab;
  };
  const stopAudio = () => {
    playbackError.hidden = true;
    audio.pause();
    if (audio.hasAttribute('src')) {
      audio.removeAttribute('src');
      audio.load();
    }
  };
  const updateListen = () => {
    listen.hidden = !active || active.mode === 'DAB';
    listen.textContent = liveAudio.playing ? t('Stop listening') : t('Listen live');
  };
  const render = (data) => {
    const initial = !ready;
    ready = true;
    active = data.tuning;
    const nextReceiverKey = JSON.stringify(data.receivers);
    if (receiverKey !== nextReceiverKey) {
      const selected = receiver.value;
      receiverKey = nextReceiverKey;
      mount(receiver, ...data.receivers.map(({ id, driver }) => h('option', { value: id },
        t('Receiver {n}', { n: id + 1 }) + (driver ? ` · ${driver}` : ''))));
      if (data.receivers.some(({ id }) => String(id) === selected)) receiver.value = selected;
    }
    if (initial) {
      mount(channel, ...data.channels.map((value) => h('option', { value }, value)));
      if (active) {
        receiver.value = active.receiver;
        mode.value = active.mode;
        if (active.channel) channel.value = active.channel;
        if (active.frequency_hz) frequency.value = active.frequency_hz / 1e6;
      }
      updateMode();
    }
    const key = JSON.stringify(active);
    if (key !== activeKey) {
      activeKey = key;
      stopAudio();
    }
    const nextStationKey = JSON.stringify(data.stations);
    if (nextStationKey !== stationKey) {
      const selected = stationList.value;
      stationKey = nextStationKey;
      mount(stationList, ...data.stations.map(({ id, name, codec }) =>
        h('option', { value: id }, codec ? `${name} · ${codec}` : name)));
      if (data.stations.some(({ id }) => String(id) === selected)) stationList.value = selected;
    }
    stations.hidden = !data.stations.length;
    const statusText = {
      idle: () => t('Ready to tune.'),
      starting: () => t('Opening receiver…'),
      discovering: () => t('Waiting for DAB stations on {channel}…', active),
      listening: () => active.mode === 'DAB'
        ? t('DAB channel {channel}', active)
        : t('Tuned to {frequency} MHz ({mode})', { frequency: active.frequency_hz / 1e6, mode: active.mode }),
    };
    const message = data.error || statusText[data.state]();
    if (status.textContent !== message) status.textContent = message;
    tune.disabled = busy;
    stop.disabled = busy || !active;
    updateListen();
  };
  const refresh = async () => {
    try {
      if (!busy) {
        const startedAt = revision;
        const data = await api.get('/api/manual');
        if (!destroyed && !busy && startedAt === revision) render(data);
      }
    } catch (e) {
      if (!destroyed) status.textContent = e.message;
    } finally {
      if (!destroyed) timer = setTimeout(refresh, 1500);
    }
  };
  const change = async (path, body) => {
    if (busy) return;
    revision++;
    busy = tune.disabled = stop.disabled = true;
    try {
      const data = await api.post(path, body);
      if (destroyed) return;
      busy = false;
      render(data);
    } catch (e) {
      if (!destroyed) errorToast(e);
    } finally {
      busy = false;
      tune.disabled = !ready;
      stop.disabled = !active;
    }
  };
  mode.addEventListener('change', updateMode);
  stop.addEventListener('click', () => change('/api/manual/stop', {}));
  play.addEventListener('click', () => {
    if (!stationList.value) return;
    if (liveAudio.playing) liveAudio.stop();
    playbackError.hidden = true;
    audio.src = `/api/manual/dab-audio?sid=${encodeURIComponent(stationList.value)}`;
    audio.play().catch(errorToast);
  });
  audio.addEventListener('error', () => {
    if (audio.hasAttribute('src')) playbackError.hidden = false;
  });
  mount(root, h('div.page.manual-tuning',
    h('div.page-head', h('h1', t('Manual tuning'))),
    h('section.card.col',
      h('form.col', { onsubmit: (event) => {
        event.preventDefault();
        change('/api/manual/tune', {
          receiver: Number(receiver.value), mode: mode.value,
          frequency_hz: Math.round(Number(frequency.value) * 1e6), channel: channel.value,
        });
      } },
      h('div.row.wrap', h('label.field', t('Receiver'), receiver), h('label.field', t('Modulation'), mode),
        frequencyField, channelField),
      h('div.row.wrap', tune, stop, listen)),
      status, stations)));
  updateMode();
  const offAudio = on('audio', updateListen);
  refresh();
  return { destroy() { destroyed = true; clearTimeout(timer); stopAudio(); offAudio(); } };
}
