import { api, errorToast, fmtMHz, h, icon, mount, t } from '../core.js';
import { liveAudio } from '../live-audio.js';
import { cfg, on } from '../state.js';
import { spectrumPainter } from './listen.js';

const analogModes = ['AM', 'NFM', 'WFM', 'SSB', 'USB', 'LSB'];

export function manualView(root) {
  const receiver = h('select', { required: true });
  const mode = h('select', {}, ...[...analogModes, 'DAB'].map((value) =>
    h('option', { value }, value === 'DAB' ? 'DAB/DAB+' : value === 'WFM' ? `${t('FM broadcast')} (WFM)` : value)));
  const frequency = h('input', {
    type: 'number', inputmode: 'decimal', min: '0.000001', max: '6000', step: '0.000001',
    value: '99.5', required: true,
  });
  const channel = h('select');
  const gainMode = h('select', {},
    h('option', { value: 'manual' }, t('Manual')),
    h('option', { value: 'auto' }, t('Auto (hardware)')),
    h('option', { value: 'sw_auto' }, t('Auto (software)')));
  const gainDb = h('input', { type: 'number', min: -100, max: 100, step: 'any', required: true });
  const gainLabel = h('span');
  const gainField = h('label.field', gainLabel, gainDb);
  const setGain = (mode, db) => {
    gainMode.value = mode;
    gainDb.value = db;
    updateGain();
  };
  function updateGain() {
    gainField.hidden = gainDb.disabled = gainMode.value === 'auto';
    gainLabel.textContent = gainMode.value === 'sw_auto' ? t('Starting gain (dB)') : t('Gain (dB)');
  }
  setGain(cfg().device.gain_mode, cfg().device.gain_db);
  const frequencyField = h('label.field', t('Frequency (MHz)'), frequency);
  const channelField = h('label.field', t('DAB channel'), channel);
  const tune = h('button.primary.big', { type: 'submit', disabled: true }, icon('play'), t('Tune'));
  const stop = h('button.big', { type: 'button', disabled: true }, icon('stop'), t('Stop tuning'));
  const listen = h('button.big', { type: 'button', hidden: true, onclick: () => liveAudio.toggle().catch(errorToast) });
  const status = h('span.chip.state', { role: 'status' }, t('Loading…'));
  const readout = h('div.freq', '—');
  const detail = h('div.freq-name');
  const rfLevel = h('span.mono');
  const meterFill = h('div.fill');
  const meter = h('div.col', { hidden: true }, h('div.meter', meterFill),
    h('div.meter-legend', h('span', '-100'), rfLevel, h('span', '0 dBFS')));
  const volume = h('input', { type: 'range', min: 0, max: 1.5, step: 0.05, value: liveAudio.volume,
    title: t('Volume'), oninput: (event) => liveAudio.setVolume(+event.target.value) });
  const analogAudio = h('div.row', { hidden: true }, listen, h('div.volume', icon('volume'), volume));
  const stationList = h('select');
  const play = h('button.primary', { type: 'button' }, t('Play station'));
  const stations = h('section.card.col', { hidden: true },
    h('div.card-head', h('h2', t('DAB/DAB+ stations'))),
    h('div.row.wrap', h('label.field', t('DAB station'), stationList), play));
  const canvas = h('canvas', { height: 260, role: 'img', 'aria-label': t('Live spectrum and waterfall around the tuned frequency.') });
  const axis = h('div.spectrum-axis');
  const spectrumCard = h('section.card.spectrum-card',
    h('div.card-head', h('h2', t('Spectrum')), h('span.faint.small', t('Click the spectrum to select a frequency'))),
    h('div.spectrum-wrap', canvas), axis);
  const spectrum = spectrumPainter(canvas, axis, t('No data. Tune to a frequency.'));
  let frame = null;

  let destroyed = false;
  let busy = false;
  let ready = false;
  let active = null;
  let playingStation = 0;
  let stationKey = '';
  let receiverKey = '';
  let revision = 0;
  let timer;
  mode.value = 'WFM';
  const updateMode = () => {
    const dab = mode.value === 'DAB';
    frequencyField.hidden = frequency.disabled = dab;
    channelField.hidden = channel.disabled = !dab;
  };
  const updateListen = () => {
    analogAudio.hidden = !active || active.mode === 'DAB';
    listen.hidden = analogAudio.hidden;
    mount(listen, icon(liveAudio.playing ? 'mute' : 'headphones'),
      liveAudio.playing ? t('Stop listening') : t('Listen live'));
    play.textContent = liveAudio.playing && playingStation === Number(stationList.value)
      ? t('Stop listening') : t('Play station');
  };
  const render = (data) => {
    const initial = !ready;
    ready = true;
    active = data.tuning;
    playingStation = data.station || 0;
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
        if (data.gain) setGain(data.gain.mode, data.gain.db);
      }
      updateMode();
    }
    const nextStationKey = JSON.stringify(data.stations);
    if (nextStationKey !== stationKey) {
      const selected = stationList.value;
      stationKey = nextStationKey;
      mount(stationList, ...data.stations.map(({ id, name, codec }) =>
        h('option', { value: id }, codec ? `${name} · ${codec}` : name)));
      if (data.stations.some(({ id }) => String(id) === selected)) stationList.value = selected;
      else if (playingStation) stationList.value = playingStation;
    }
    stations.hidden = !data.stations.length;
    spectrumCard.hidden = active?.mode === 'DAB';
    readout.textContent = active?.mode === 'DAB' ? active.channel
      : active ? `${fmtMHz(active.frequency_hz, 3)} MHz` : '—';
    detail.textContent = active?.mode === 'DAB' ? 'DAB/DAB+' : active?.mode === 'WFM' ? t('FM broadcast') : active?.mode || '';
    meter.hidden = data.level == null || !active || active.mode === 'DAB';
    if (!meter.hidden) {
      meterFill.style.width = `${Math.max(0, Math.min(100, 100 + data.level))}%`;
      rfLevel.textContent = `${data.level.toFixed(1)} dBFS`;
    }
    if (data.spectrum || frame) {
      frame = data.spectrum ? { ...data.spectrum, tuned: active?.frequency_hz } : null;
      spectrum.draw(frame);
    }
    const statusText = {
      idle: () => t('Ready to tune.'),
      starting: () => t('Opening receiver…'),
      discovering: () => t('Waiting for DAB stations on {channel}…', active),
      listening: () => active.mode === 'DAB'
        ? t('DAB channel {channel}', active)
        : t('Tuned to {frequency} MHz ({mode})', { frequency: active.frequency_hz / 1e6, mode: active.mode }),
    };
    const message = data.error || statusText[data.state]();
    if (status.textContent !== message) mount(status, h('span.dot'), message);
    status.className = `chip state ${data.error ? 'bad' : data.state === 'listening' ? 'good live' : active ? 'accent' : ''}`;
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
      if (!destroyed) timer = setTimeout(refresh, active && active.mode !== 'DAB' ? 500 : 1500);
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
      return true;
    } catch (e) {
      if (!destroyed) errorToast(e);
    } finally {
      busy = false;
      tune.disabled = !ready;
      stop.disabled = !active;
    }
  };
  mode.addEventListener('change', updateMode);
  gainMode.addEventListener('change', updateGain);
  canvas.addEventListener('click', (event) => {
    if (!frame) return;
    mode.value = mode.value === 'DAB' ? 'WFM' : mode.value;
    updateMode();
    const hz = frame.f0 + (frame.f1 - frame.f0) * event.offsetX / canvas.clientWidth;
    frequency.value = (hz / 1e6).toFixed(6);
    frequency.focus();
  });
  stop.addEventListener('click', () => change('/api/manual/stop', {}));
  stationList.addEventListener('change', updateListen);
  play.addEventListener('click', async () => {
    const sid = Number(stationList.value);
    if (!sid) return;
    if (liveAudio.playing && playingStation === sid) {
      liveAudio.stop();
      return;
    }
    const wasPlaying = liveAudio.playing;
    try {
      const [, selected] = await Promise.all([wasPlaying ? null : liveAudio.start(), change('/api/manual/dab-station', { sid })]);
      if (!selected && !wasPlaying) liveAudio.stop();
    } catch (error) {
      if (!wasPlaying) liveAudio.stop();
      errorToast(error);
    }
  });
  mount(root, h('div.page.manual-tuning',
    h('div.page-head', h('h1', t('Manual tuning'))),
    h('div.grid.listen',
    h('div.col',
    h('section.card.hero', h('div.row', status), h('div', readout, detail), meter,
      h('form.col', { onsubmit: (event) => {
        event.preventDefault();
        change('/api/manual/tune', {
          receiver: Number(receiver.value), mode: mode.value,
          frequency_hz: Math.round(Number(frequency.value) * 1e6), channel: channel.value,
          gain_mode: gainMode.value, gain_db: Number(gainDb.value),
        });
      } },
      h('div.row.wrap', h('label.field', t('Modulation'), mode),
        frequencyField, channelField),
      h('div.row.wrap', h('label.field', t('Gain mode'), gainMode), gainField),
      h('div.controls', tune, stop, analogAudio))),
    spectrumCard),
    h('div.col',
      h('section.card.col', h('div.card-head', h('h2', t('SDR device'))), h('label.field', t('Receiver'), receiver)),
      stations))));
  updateMode();
  const offAudio = on('audio', updateListen);
  refresh();
  return { destroy() { destroyed = true; clearTimeout(timer); offAudio(); spectrum.destroy(); } };
}
