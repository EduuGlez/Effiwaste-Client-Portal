document.addEventListener('DOMContentLoaded', () => {
  const openSidebar = () => document.body.classList.add('sidebar-open');
  const closeSidebar = () => document.body.classList.remove('sidebar-open');
  document.querySelector('[data-sidebar-open]')?.addEventListener('click', openSidebar);
  document.querySelector('[data-sidebar-close]')?.addEventListener('click', closeSidebar);
  document.querySelectorAll('.sidebar-nav a').forEach((link) => link.addEventListener('click', closeSidebar));

  const passwordToggle = document.querySelector('[data-password-toggle]');
  passwordToggle?.addEventListener('click', () => {
    const input = document.getElementById('login-password');
    if (!input) return;
    const visible = input.type === 'text';
    input.type = visible ? 'password' : 'text';
    passwordToggle.setAttribute('aria-label', visible ? 'Mostrar contraseña' : 'Ocultar contraseña');
    passwordToggle.classList.toggle('is-visible', !visible);
  });

  document.querySelectorAll('[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const tabButtons = document.querySelectorAll('[data-admin-tab]');
  tabButtons.forEach((button) => button.addEventListener('click', () => {
    tabButtons.forEach((item) => item.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach((item) => item.classList.remove('active'));
    button.classList.add('active');
    document.querySelector(`#tab-${button.dataset.adminTab}`)?.classList.add('active');
  }));

  document.querySelectorAll('[data-open-panel]').forEach((button) => button.addEventListener('click', () => {
    const modal = document.getElementById(button.dataset.openPanel);
    modal?.classList.add('open');
    modal?.setAttribute('aria-hidden', 'false');
  }));
  document.querySelectorAll('[data-close-panel]').forEach((button) => button.addEventListener('click', () => {
    const modal = button.closest('.modal');
    modal?.classList.remove('open');
    modal?.setAttribute('aria-hidden', 'true');
  }));

  const updateAudience = () => {
    const selected = document.querySelector('input[name="audience"]:checked')?.value;
    document.getElementById('chain-field')?.classList.toggle('visible', selected === 'chain');
    document.getElementById('hotel-field')?.classList.toggle('visible', selected === 'hotel');
  };
  document.querySelectorAll('input[name="audience"]').forEach((radio) => radio.addEventListener('change', updateAudience));
  updateAudience();

  const categorySelect = document.getElementById('document-category');
  const updateDocumentCategory = () => {
    const monthly = categorySelect?.value === 'monthly_report';
    document.getElementById('report-month-field')?.classList.toggle('visible', monthly);
    const monthInput = document.getElementById('report-month');
    if (monthInput) monthInput.required = monthly;
    if (monthly) {
      const hotelAudience = document.querySelector('input[name="audience"][value="hotel"]');
      if (hotelAudience) hotelAudience.checked = true;
      updateAudience();
    }
  };
  categorySelect?.addEventListener('change', updateDocumentCategory);
  updateDocumentCategory();

  const fileInput = document.getElementById('pdf-input');
  fileInput?.addEventListener('change', () => {
    const target = document.getElementById('file-name');
    if (target && fileInput.files[0]) target.textContent = `${fileInput.files[0].name} · ${(fileInput.files[0].size / 1048576).toFixed(2)} MB`;
  });

  const chatForm = document.getElementById('chat-form');
  if (chatForm) initializeChat(chatForm);

  const actionItems = [...document.querySelectorAll('[data-action-id]')];
  const activateAction = (id) => {
    actionItems.forEach((item) => item.classList.toggle('active', item.dataset.actionId === id));
    document.querySelectorAll('.action-detail-panel').forEach((panel) => {
      panel.classList.toggle('active', panel.id === `action-detail-${id}`);
    });
  };
  actionItems.forEach((item) => item.addEventListener('click', () => activateAction(item.dataset.actionId)));

  document.getElementById('action-library-search')?.addEventListener('input', (event) => {
    const query = event.target.value.trim().toLocaleLowerCase('es');
    let firstVisible = null;
    let visibleCount = 0;
    actionItems.forEach((item) => {
      const visible = !query || item.dataset.actionSearch.toLocaleLowerCase('es').includes(query);
      item.hidden = !visible;
      if (visible) { visibleCount += 1; firstVisible ||= item; }
    });
    document.getElementById('action-search-empty')?.classList.toggle('visible', visibleCount === 0);
    const activeVisible = actionItems.some((item) => item.classList.contains('active') && !item.hidden);
    if (!activeVisible && firstVisible) activateAction(firstVisible.dataset.actionId);
  });

  initializeDocumentLibrary();

  if (document.querySelector('.status-processing, .status-queued')) {
    window.setTimeout(() => window.location.reload(), 10000);
  }
});

function initializeDocumentLibrary() {
  const grid = document.getElementById('document-library-grid');
  if (!grid) return;
  const cards = [...grid.querySelectorAll('[data-library-document]')];
  const search = document.getElementById('document-library-search');
  const sort = document.getElementById('document-library-sort');
  const filters = [...document.querySelectorAll('[data-library-category]')];
  const empty = document.getElementById('document-library-empty');
  const counter = document.getElementById('library-results-count');
  const title = document.getElementById('library-results-title');
  const clear = document.querySelector('[data-library-clear]');
  let category = 'all';

  const normalize = (value) => value
    .toLocaleLowerCase('es')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '');

  const compareCards = (left, right) => {
    const mode = sort?.value || 'recent';
    if (mode === 'oldest') return left.dataset.created.localeCompare(right.dataset.created);
    if (mode === 'name-asc') return left.dataset.name.localeCompare(right.dataset.name, 'es');
    if (mode === 'name-desc') return right.dataset.name.localeCompare(left.dataset.name, 'es');
    if (mode === 'category') {
      return left.dataset.categoryLabel.localeCompare(right.dataset.categoryLabel, 'es')
        || left.dataset.name.localeCompare(right.dataset.name, 'es');
    }
    return right.dataset.created.localeCompare(left.dataset.created);
  };

  const update = () => {
    const query = normalize(search?.value.trim() || '');
    const ordered = [...cards].sort(compareCards);
    let visible = 0;
    ordered.forEach((card) => {
      const categoryMatch = category === 'all' || card.dataset.category === category;
      const searchMatch = !query || normalize(card.dataset.name).includes(query);
      card.hidden = !(categoryMatch && searchMatch);
      if (!card.hidden) visible += 1;
      grid.appendChild(card);
    });
    if (counter) counter.textContent = `${visible} ${visible === 1 ? 'resultado' : 'resultados'}`;
    if (empty) empty.hidden = visible !== 0;
    clear?.classList.toggle('visible', Boolean(search?.value));
  };

  filters.forEach((button) => button.addEventListener('click', () => {
    category = button.dataset.libraryCategory;
    filters.forEach((item) => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    if (title) title.textContent = category === 'all' ? 'Todos los documentos' : button.querySelector('span').textContent;
    update();
  }));
  search?.addEventListener('input', update);
  sort?.addEventListener('change', update);
  clear?.addEventListener('click', () => {
    search.value = '';
    search.focus();
    update();
  });
  update();
}

function initializeChat(form) {
  const input = document.getElementById('question');
  const messages = document.getElementById('chat-messages');
  const shell = form.closest('.chat-shell');
  const submit = form.querySelector('button[type="submit"]');
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';

  const resize = () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 120)}px`; };
  input.addEventListener('input', resize);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });
  document.querySelectorAll('[data-question]').forEach((button) => button.addEventListener('click', () => {
    input.value = button.dataset.question; form.requestSubmit();
  }));

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (question.length < 2 || submit.disabled) return;
    shell.classList.add('has-messages');
    appendUser(messages, question);
    input.value = ''; resize(); submit.disabled = true;
    const loading = appendLoading(messages);
    messages.scrollTop = messages.scrollHeight;

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ question })
      });
      if (response.redirected) { window.location.href = response.url; return; }
      const data = await response.json();
      loading.remove();
      if (!response.ok) throw new Error(data.detail || 'No se pudo obtener una respuesta.');
      appendAssistant(messages, data.answer, data.sources || [], false, data.answer_html);
    } catch (error) {
      loading.remove(); appendAssistant(messages, error.message, [], true);
    } finally {
      submit.disabled = false; input.focus(); messages.scrollTop = messages.scrollHeight;
    }
  });
}

function appendUser(container, text) {
  const item = document.createElement('div'); item.className = 'message message-user'; item.textContent = text; container.appendChild(item);
}

function appendLoading(container) {
  const item = document.createElement('div'); item.className = 'message message-assistant';
  item.innerHTML = '<div class="assistant-label"><span>✦</span> EFFIWASTE AI</div><div class="thinking"><i></i><i></i><i></i></div>';
  container.appendChild(item); return item;
}

function appendAssistant(container, answer, sources, isError = false, answerHtml = '') {
  const item = document.createElement('div'); item.className = 'message message-assistant';
  const label = document.createElement('div'); label.className = 'assistant-label'; label.innerHTML = '<span>✦</span> EFFIWASTE AI'; item.appendChild(label);
  const text = document.createElement('div'); text.className = 'answer-text';
  if (isError) {
    text.style.color = '#a13d3d';
    text.textContent = answer;
  } else if (answerHtml) {
    text.innerHTML = answerHtml;
  } else {
    text.textContent = answer;
  }
  item.appendChild(text);
  if (sources.length) {
    const details = document.createElement('details'); details.className = 'sources';
    const summary = document.createElement('summary'); summary.textContent = `${sources.length} fragmentos consultados`; details.appendChild(summary);
    const list = document.createElement('div'); list.className = 'source-list';
    sources.forEach((source, index) => {
      const card = document.createElement('article'); card.className = 'source-card';
      const title = document.createElement('strong'); title.textContent = `[${index + 1}] ${source.document_name}`;
      const meta = document.createElement('small'); meta.textContent = `Página ${source.page_number}`;
      const excerpt = document.createElement('p'); excerpt.textContent = source.content.length > 360 ? `${source.content.slice(0, 360)}…` : source.content;
      card.append(title, meta, excerpt); list.appendChild(card);
    });
    details.appendChild(list); item.appendChild(details);
  }
  container.appendChild(item);
}
