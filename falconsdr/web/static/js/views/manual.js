import { api, errorToast, fmtMHz, h, icon, mount, seg, slider, t } from '../core.js';
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
  let gainMode = cfg().device.gain_mode;
  let gainDb = cfg().device.gain_db;
  let receivers = [];
  const gainField = h('div.col');
  const updateGain = () => {
    const device = receivers.find(({ id }) => id === Number(receiver.value));
    const [min, max] = device?.gain_range || [0, 50];
    gainDb = Math.max(min, Math.min(max, gainDb));
    const name = device?.gain_index ? t('Sensitivity index')
      : gainMode === 'sw_auto' ? t('Starting gain') : t('Gain');
    const level = gainMode === 'auto' ? null : slider({
      name, min, max, step: device?.gain_index ? 1 : 0.1, value: gainDb,
      unit: device?.gain_index ? '' : 'dB', oninput: (value) => { gainDb = value; },
    });
    if (level) {
      gainDb = Number(level.input.value);
      level.set(gainDb);
      level.input.setAttribute('aria-label', name);
    }
    mount(gainField, h('span.small.muted', t('Gain')),
      seg([['manual', t('Manual')], ['auto', t('Auto (hardware)')], ['sw_auto', t('Auto (software)')]],
        gainMode, (value) => { gainMode = value; updateGain(); }), level);
  };
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
  const audio = h('div.row', { hidden: true }, listen, h('div.volume', icon('volume'), volume));
  const stationRows = h('ul.manual-stations');
  const stationTitle = h('h2');
  const stationEmpty = h('p.muted', t('No favourites saved.'));
  const stations = h('section.card.col', { hidden: true },
    h('div.card-head', stationTitle), stationRows, stationEmpty);
  const stationInfo = h('div.col');
  const info = h('section.card.col', { hidden: true },
    h('div.card-head', h('h2', t('Station information'))), stationInfo);
  const canvas = h('canvas', { height: 260, role: 'img', 'aria-label': t('Live spectrum and waterfall around the tuned frequency.') });
  const axis = h('div.spectrum-axis');
  const spectrumHint = h('span.faint.small', t('Click the spectrum to select a frequency'));
  const spectrumCard = h('section.card.spectrum-card',
    h('div.card-head', h('h2', t('Spectrum')), spectrumHint),
    h('div.spectrum-wrap', canvas), axis);
  const spectrum = spectrumPainter(canvas, axis, t('No data. Tune to a frequency.'));
  let frame = null;

  let destroyed = false;
  let busy = false;
  let ready = false;
  let active = null;
  let playingStation = 0;
  let stationKey = '';
  let infoKey = '';
  let latest;
  let receiverKey = '';
  let revision = 0;
  let timer;
  mode.value = 'WFM';
  const tuneValues = () => ({
    receiver: Number(receiver.value), mode: mode.value,
    frequency_hz: Math.round(Number(frequency.value) * 1e6), channel: channel.value,
    gain_mode: gainMode, gain_db: gainDb,
  });
  const updateControls = () => {
    tune.disabled = !ready || busy || (mode.value === 'DAB' && channel.value === 'favourites');
    stop.disabled = busy || !active;
  };
  const formatFM = (hz) => `${fmtMHz(hz, 3)} MHz`;
  const matches = (a, b) => a.mode === b.mode && (a.mode === 'DAB'
    ? a.channel === b.channel && a.id === b.id : a.frequency_hz === b.frequency_hz);
  const isFavourite = (entry) => (latest.favourites || []).some((fav) => matches(entry, fav));
  const favouriteButton = (entry) => {
    const saved = isFavourite(entry);
    const label = saved ? t('Remove from favourites') : t('Add to favourites');
    return h('button.manual-star', {
      type: 'button', title: label, 'aria-label': label, 'aria-pressed': String(saved),
      onclick: () => change('/api/manual/favourite', { ...entry, sid: entry.id, enabled: !saved }),
    }, saved ? '★' : '☆');
  };
  const playEntry = async (entry, current) => {
    if (busy) return;
    if (current && liveAudio.playing) {
      liveAudio.stop();
      return;
    }
    const dab = entry.mode === 'DAB';
    liveAudio.useDab(dab);
    const wasPlaying = liveAudio.playing;
    const action = dab && active?.mode === 'DAB' && active.channel === entry.channel
        && latest.stations.some(({ id }) => id === entry.id)
      ? change('/api/manual/dab-station', { sid: entry.id })
      : change('/api/manual/tune', { ...tuneValues(), ...entry, station: dab ? entry.id : 0 });
    try {
      const [, ok] = await Promise.all([wasPlaying ? null : liveAudio.start(), action]);
      if (!ok && !wasPlaying) liveAudio.stop();
    } catch (e) {
      if (!wasPlaying) liveAudio.stop();
      errorToast(e);
    }
  };
  const updateStationCards = () => {
    if (!latest) return;
    const showingDabFavs = mode.value === 'DAB' && channel.value === 'favourites';
    const showingFM = mode.value === 'WFM';
    const tunedDab = mode.value === 'DAB' && active?.mode === 'DAB' && channel.value === active.channel;
    const rows = showingDabFavs || showingFM
      ? (latest.favourites || []).filter((entry) => entry.mode === mode.value)
      : tunedDab ? latest.stations.map((entry) => ({ ...entry, mode: 'DAB', channel: active.channel })) : [];
    stations.hidden = !showingDabFavs && !showingFM && rows.length === 0;
    stationTitle.textContent = showingDabFavs || showingFM ? t('Favourites') : t('DAB/DAB+ stations');
    stationEmpty.hidden = rows.length > 0;
    const nextStationKey = JSON.stringify([rows, latest.favourites, active, playingStation, liveAudio.playing]);
    if (nextStationKey !== stationKey) {
      stationKey = nextStationKey;
      mount(stationRows, ...rows.map((entry) => {
        const dab = entry.mode === 'DAB';
        const current = active?.mode === entry.mode && (dab
          ? active.channel === entry.channel && playingStation === entry.id
          : active.frequency_hz === entry.frequency_hz);
        const label = entry.name || formatFM(entry.frequency_hz);
        const sub = dab ? `${entry.channel} · ${entry.codec}` : entry.name && formatFM(entry.frequency_hz);
        const playing = current && liveAudio.playing;
        return h('li.manual-station', h('button.manual-station-play', {
          type: 'button', 'aria-label': `${playing ? t('Stop listening') : t('Play station')}: ${label}`,
          onclick: () => playEntry(entry, current),
        }, icon(playing ? 'stop' : 'play'), h('span.col', h('strong', label),
          sub ? h('small.muted', sub) : null)), favouriteButton(entry));
      }));
    }
    const dab = active?.mode === 'DAB';
    const fm = active?.mode === 'WFM';
    info.hidden = !dab && !fm;
    const meta = latest.station_info || {};
    const service = dab && latest.stations.find(({ id }) => id === playingStation);
    const fmEntry = fm && { mode: 'WFM', frequency_hz: active.frequency_hz };
    const nextInfoKey = JSON.stringify([meta, service, fmEntry, fm && isFavourite(fmEntry)]);
    if (nextInfoKey !== infoKey) {
      infoKey = nextInfoKey;
      const title = dab ? service?.name : (meta.name || (fm && formatFM(active.frequency_hz)));
      mount(stationInfo,
        title || fm ? h('div.row', h('strong', title), h('span.spacer'), fm && favouriteButton(fmEntry)) : null,
        meta.ensemble ? h('span.muted', `${t('Ensemble')}: ${meta.ensemble}`) : null,
        service?.codec ? h('span.muted', service.codec) : null,
        meta.pi ? h('span.muted', `RDS PI: ${meta.pi}`) : null,
        meta.text ? h('span', meta.text) : null);
    }
    info.hidden ||= !stationInfo.childElementCount;
  };
  const updateMode = () => {
    const dab = mode.value === 'DAB';
    frequencyField.hidden = frequency.disabled = dab;
    channelField.hidden = channel.disabled = !dab;
    updateControls();
    updateStationCards();
  };
  const updateListen = () => {
    audio.hidden = !active;
    listen.hidden = !active || active.mode === 'DAB';
    mount(listen, icon(liveAudio.playing ? 'mute' : 'headphones'),
      liveAudio.playing ? t('Stop listening') : t('Listen live'));
    updateStationCards();
  };
  const render = (data) => {
    latest = data;
    const initial = !ready;
    ready = true;
    active = data.tuning;
    playingStation = data.station || 0;
    const previousReceiverKey = receiverKey;
    const nextReceiverKey = JSON.stringify(data.receivers);
    if (receiverKey !== nextReceiverKey) {
      const selected = receiver.value;
      receiverKey = nextReceiverKey;
      receivers = data.receivers;
      mount(receiver, ...data.receivers.map(({ id, driver }) => h('option', { value: id },
        t('Receiver {n}', { n: id + 1 }) + (driver ? ` · ${driver}` : ''))));
      if (data.receivers.some(({ id }) => String(id) === selected)) receiver.value = selected;
    }
    if (initial) {
      mount(channel, h('option', { value: 'favourites' }, t('Favourites')),
        ...data.channels.map((value) => h('option', { value }, value)));
      if (data.channels.length) channel.value = data.channels[0];
      if (active) {
        receiver.value = active.receiver;
        mode.value = active.mode;
        if (active.channel) channel.value = active.channel;
        if (active.mode !== 'DAB' && active.frequency_hz) frequency.value = active.frequency_hz / 1e6;
        if (data.gain) {
          gainMode = data.gain.mode;
          gainDb = data.gain.db;
        }
      }
      updateMode();
    }
    if (initial || receiverKey !== previousReceiverKey) updateGain();
    spectrumHint.hidden = active?.mode === 'DAB';
    readout.textContent = active?.mode === 'DAB' ? active.channel
      : active ? `${fmtMHz(active.frequency_hz, 3)} MHz` : '—';
    detail.textContent = active?.mode === 'DAB' ? 'DAB/DAB+' : active?.mode === 'WFM' ? t('FM broadcast') : active?.mode || '';
    meter.hidden = data.level == null || !active;
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
    updateControls();
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
      updateControls();
    }
  };
  mode.addEventListener('change', updateMode);
  channel.addEventListener('change', updateMode);
  receiver.addEventListener('change', updateGain);
  canvas.addEventListener('click', (event) => {
    if (!frame || active?.mode === 'DAB') return;
    mode.value = mode.value === 'DAB' ? 'WFM' : mode.value;
    updateMode();
    const hz = frame.f0 + (frame.f1 - frame.f0) * event.offsetX / canvas.clientWidth;
    frequency.value = (hz / 1e6).toFixed(6);
    frequency.focus();
  });
  stop.addEventListener('click', async () => {
    if (await change('/api/manual/stop', {})) {
      liveAudio.stop();
      liveAudio.useDab(false);
    }
  });
  mount(root, h('div.page.manual-tuning',
    h('div.page-head', h('h1', t('Manual tuning'))),
    h('div.grid.listen',
    h('div.col',
    h('section.card.hero', h('div.row', status), h('div', readout, detail), meter,
      h('form.col', { onsubmit: (event) => {
        event.preventDefault();
        if (!tune.disabled) change('/api/manual/tune', tuneValues());
      } },
      h('div.row.wrap', h('label.field', t('Modulation'), mode),
        frequencyField, channelField),
      h('div.controls', tune, stop, audio))),
    spectrumCard),
    h('div.col',
      h('section.card.col', h('div.card-head', h('h2', t('SDR device'))), h('label.field', t('Receiver'), receiver), gainField),
      stations, info))));
  updateMode();
  const offAudio = on('audio', updateListen);
  refresh();
  return { destroy() { destroyed = true; clearTimeout(timer); offAudio(); spectrum.destroy(); } };
}
