// Portable Memory List backups. Frequencies are actual carrier Hz, not 8.33 channel labels.
import { t } from './core.js';

const FIELDS = ['freq_hz', 'name', 'modulation', 'enabled', 'folder', 'weight', 'ch833'];
const MODULATIONS = new Set(['AM', 'NFM', 'WFM', 'SSB', 'USB', 'LSB', 'DMR', 'YSF']);
export const MAX_CHANNEL_FILE_BYTES = 5 * 1024 * 1024;

export function exportChannelsXml(channels, folders) {
  const doc = document.implementation.createDocument(null, 'alisdr-channels');
  const root = doc.documentElement;
  root.setAttribute('version', '1');
  const folderList = root.appendChild(doc.createElement('folders'));
  for (const name of new Set([...folders, ...channels.map(c => c.folder)].filter(Boolean))) {
    const node = folderList.appendChild(doc.createElement('folder'));
    node.setAttribute('name', name);
  }
  const list = root.appendChild(doc.createElement('channels'));
  for (const channel of channels) {
    const node = list.appendChild(doc.createElement('channel'));
    for (const key of FIELDS) node.setAttribute(key, String(channel[key] ?? (key === 'weight' ? 0 : key === 'ch833' ? false : '')));
  }
  return '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(doc) + '\n';
}

export function parseChannelsXml(text) {
  if (/<!DOCTYPE|<!ENTITY/i.test(text)) throw new Error(t('Use an AliSDR channel XML file without a document type or entities.'));
  const doc = new DOMParser().parseFromString(text, 'application/xml');
  const root = doc.documentElement;
  if (doc.querySelector('parsererror') || root.tagName !== 'alisdr-channels' || root.getAttribute('version') !== '1'
      || [...root.children].some(node => !['folders', 'channels'].includes(node.tagName))
      || root.getElementsByTagName('channels').length !== 1 || root.getElementsByTagName('folders').length > 1) {
    throw new Error(t('This is not a supported AliSDR channel XML file.'));
  }
  const channels = [], seen = new Set(), folders = new Set();
  for (const node of root.querySelectorAll('folders > folder')) {
    const name = (node.getAttribute('name') || '').trim();
    if (name) folders.add(name);
  }
  for (const [i, node] of [...root.querySelector('channels').children].entries()) {
    const number = (key, fallback) => {
      const value = node.getAttribute(key);
      return value === null ? fallback : value.trim() ? Number(value) : NaN;
    };
    const boolean = (key, fallback) => {
      const value = node.getAttribute(key);
      return value === null ? fallback : ['true', '1'].includes(value) ? true : ['false', '0'].includes(value) ? false : null;
    };
    let freq_hz = number('freq_hz', NaN);
    const channel = {
      freq_hz, name: (node.getAttribute('name') || '').trim(), modulation: node.getAttribute('modulation') || 'AM',
      enabled: boolean('enabled', true), folder: (node.getAttribute('folder') || '').trim(),
      weight: number('weight', 0), ch833: boolean('ch833', false),
    };
    if (node.tagName !== 'channel' || !Number.isSafeInteger(freq_hz) || freq_hz <= 0
        || !MODULATIONS.has(channel.modulation) || channel.enabled === null || channel.ch833 === null
        || !Number.isFinite(channel.weight) || channel.weight < 0) {
      throw new Error(t('Invalid channel data at entry {n}.', { n: i + 1 }));
    }
    // Mirror models.snap_airband_hz so aliases cannot overwrite a named channel without a prompt.
    if (freq_hz >= 118e6 && freq_hz <= 137e6 && Math.abs(freq_hz - 121500000) >= 1500) {
      const block = Math.floor(freq_hz / 25000) * 25000;
      const snapped = block + Math.round((freq_hz - block) / (25000 / 3)) * 25000 / 3;
      if (Math.abs(snapped - freq_hz) <= 1500) channel.freq_hz = Math.round(snapped);
    }
    freq_hz = channel.freq_hz;
    if (seen.has(freq_hz)) throw new Error(t('The XML file contains duplicate frequencies. No channels were imported.'));
    seen.add(freq_hz);
    if (channel.folder) folders.add(channel.folder);
    channels.push(channel);
  }
  return { channels, folders: [...folders] };
}

export function planChannelImport(existing, incoming) {
  const byFrequency = new Map(existing.map(channel => [channel.freq_hz, channel]));
  const added = [], conflicts = [];
  let unchanged = 0;
  for (const channel of incoming) {
    const current = byFrequency.get(channel.freq_hz);
    if (!current) added.push(channel);
    else if (FIELDS.every(key => current[key] === channel[key])) unchanged++;
    else conflicts.push({ current, incoming: channel });
  }
  return { added, conflicts, unchanged };
}
