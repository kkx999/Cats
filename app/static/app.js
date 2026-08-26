document.addEventListener('DOMContentLoaded', () => {
  document.querySelector('[data-sidebar]')?.addEventListener('click', () => {
    document.querySelector('#sidebar')?.classList.toggle('open');
  });

  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      await navigator.clipboard.writeText(button.dataset.copy);
      const label = button.querySelector('small');
      if (label) { label.textContent = '已复制'; setTimeout(() => label.textContent = '点击复制', 1300); }
    });
  });

  const form = document.querySelector('#task-form');
  if (!form) return;
  const textInput = document.querySelector('#message-text');
  const previewText = document.querySelector('#preview-text');
  const previewMedia = document.querySelector('#preview-media');
  const mediaSelect = document.querySelector('#media-id');
  const builder = document.querySelector('#button-builder');
  const buttonsInput = document.querySelector('#buttons-json');

  const updatePreview = () => {
    previewText.textContent = textInput.value || '消息内容会显示在这里';
    previewMedia.hidden = !mediaSelect.value;
    const rows = readButtons();
    document.querySelector('#preview-buttons').innerHTML = rows.flat().map(item => `<div class="preview-button">${escapeHtml(item.text)}</div>`).join('');
  };
  const escapeHtml = (value) => value.replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const readButtons = () => [...builder.querySelectorAll('.button-edit-row')].map(row => [{text: row.querySelector('[data-label]').value.trim(), url: row.querySelector('[data-url]').value.trim()}]).filter(row => row[0].text && row[0].url);
  const syncButtons = () => { buttonsInput.value = JSON.stringify(readButtons()); updatePreview(); };
  const addButton = (item = {text:'', url:''}) => {
    const row = document.createElement('div'); row.className = 'button-edit-row';
    row.innerHTML = `<input data-label maxlength="64" placeholder="按钮文字" value="${escapeHtml(item.text || '')}"><input data-url placeholder="https://…" value="${escapeHtml(item.url || '')}"><button class="remove-button" type="button">×</button>`;
    row.querySelectorAll('input').forEach(input => input.addEventListener('input', syncButtons));
    row.querySelector('button').addEventListener('click', () => { row.remove(); syncButtons(); });
    builder.appendChild(row);
  };
  let initial = [];
  try { initial = JSON.parse(buttonsInput.value || '[]').flat(); } catch (_) {}
  initial.forEach(addButton);
  document.querySelector('#add-button').addEventListener('click', () => addButton());
  textInput.addEventListener('input', updatePreview);
  mediaSelect.addEventListener('change', updatePreview);

  const scheduleChanged = () => {
    const value = form.querySelector('[name=schedule_kind]:checked')?.value;
    document.querySelector('#interval-fields').classList.toggle('visible', value === 'interval');
  };
  form.querySelectorAll('[name=schedule_kind]').forEach(input => input.addEventListener('change', scheduleChanged));
  scheduleChanged();

  const startInput = form.querySelector('[name=start_at]');
  if (!startInput.value) {
    const start = new Date(Date.now() + 5 * 60 * 1000);
    start.setMinutes(start.getMinutes() - start.getTimezoneOffset());
    startInput.value = start.toISOString().slice(0, 16);
  }

  document.querySelector('#media-upload').addEventListener('change', async (event) => {
    const file = event.target.files[0]; if (!file) return;
    const progress = document.querySelector('#upload-progress'); progress.hidden = false;
    const data = new FormData(); data.append('file', file);
    try {
      const response = await fetch('/api/media', {method:'POST', body:data});
      if (!response.ok) throw new Error((await response.json()).detail || '上传失败');
      const item = await response.json();
      const option = new Option(`${item.name} · ${item.type === 'photo' ? '图片' : '视频'}`, item.id, true, true);
      mediaSelect.add(option); updatePreview();
    } catch (error) { alert(error.message); }
    finally { progress.hidden = true; event.target.value = ''; }
  });

  form.addEventListener('submit', syncButtons);
  updatePreview();
});
