// Front da Val: consome a API do agente implantado (/api/chat e /api/info).
// A conversa mora aqui no navegador; a API não guarda estado.

const $ = (seletor) => document.querySelector(seletor);
const lista = $("#mensagens");
const form = $("#form");
const entrada = $("#entrada");
const enviar = $("#enviar");
const status = $("#status");

let historico = [];
let ocupado = false;

function idDeSessao() {
  // A sessão decide a versão do prompt no canary: a mesma pessoa fica na mesma versão.
  try {
    let id = localStorage.getItem("val-sessao");
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem("val-sessao", id);
    }
    return id;
  } catch {
    return crypto.randomUUID();
  }
}
const sessaoId = idDeSessao();

// ---------- texto seguro: escapa HTML e aplica um markdown mínimo ----------
function escapar(texto) {
  return texto.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const ITEM = /^\s*([-*•]|\d+[.)])\s+/;

function formatar(texto) {
  // Agrupa linhas seguidas do mesmo tipo: itens viram lista, o resto vira parágrafo.
  const inline = (l) => l.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const html = [];
  let grupo = [];
  let tipo = null;
  const fechar = () => {
    if (!grupo.length) return;
    if (tipo === "item") {
      const tag = /^\s*\d/.test(grupo[0]) ? "ol" : "ul";
      html.push(`<${tag}>${grupo.map((l) => `<li>${inline(l.replace(ITEM, ""))}</li>`).join("")}</${tag}>`);
    } else {
      html.push(`<p>${grupo.map(inline).join("<br>")}</p>`);
    }
    grupo = [];
  };
  for (const linha of escapar(texto).split("\n")) {
    if (!linha.trim()) {
      fechar();
      tipo = null;
      continue;
    }
    const atual = ITEM.test(linha) ? "item" : "texto";
    if (atual !== tipo) fechar();
    tipo = atual;
    grupo.push(linha);
  }
  fechar();
  return html.join("");
}

// ---------- recibos: o que o agente fez para chegar na resposta ----------
function recibo(ferramenta) {
  const { nome, argumentos, resultado } = ferramenta;
  if (nome === "consultar_base") {
    const ids = (resultado.artigos || []).map((a) => a.id);
    return { texto: ids.length ? `consultou a base · ${ids.join(", ")}` : "consultou a base · nada encontrado" };
  }
  if (nome === "abrir_chamado") {
    return { texto: resultado.protocolo ? `abriu chamado ${resultado.protocolo}` : "tentou abrir chamado", classe: "chamado" };
  }
  if (nome === "somar") {
    return { texto: `somou ${argumentos.a} + ${argumentos.b} = ${resultado.resultado}` };
  }
  return { texto: nome };
}

function adicionar(papel, texto, extra = {}) {
  $("#vazio")?.remove();
  const li = document.createElement("li");
  li.className = `msg ${papel}`;
  const balao = document.createElement("div");
  balao.className = "balao";
  balao.innerHTML = formatar(texto);
  li.append(balao);

  if (extra.ferramentas?.length) {
    const recibos = document.createElement("div");
    recibos.className = "recibos";
    for (const f of extra.ferramentas) {
      const r = recibo(f);
      const span = document.createElement("span");
      span.className = `recibo ${r.classe || ""}`;
      span.textContent = r.texto;
      recibos.append(span);
    }
    li.append(recibos);
  }
  if (extra.meta) {
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = extra.meta;
    li.append(meta);
  }
  lista.append(li);
  lista.scrollTop = lista.scrollHeight;
  return li;
}

function mostrarDigitando() {
  const li = document.createElement("li");
  li.className = "msg val digitando";
  li.innerHTML = '<div class="balao" aria-label="Val está escrevendo"><i></i><i></i><i></i></div>';
  lista.append(li);
  lista.scrollTop = lista.scrollHeight;
  return li;
}

async function perguntar(texto) {
  if (ocupado || !texto.trim()) return;
  ocupado = true;
  enviar.disabled = true;
  adicionar("usuario", texto);
  historico.push({ role: "user", content: texto });
  const digitando = mostrarDigitando();

  try {
    const resposta = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mensagens: historico, sessao_id: sessaoId }),
    });
    const corpo = await resposta.json().catch(() => ({}));
    if (!resposta.ok) throw new Error(typeof corpo.detail === "string" ? corpo.detail : `Erro ${resposta.status}`);

    historico.push({ role: "assistant", content: corpo.resposta });
    digitando.remove();
    const segundos = (corpo.latencia_ms / 1000).toFixed(1).replace(".", ",");
    adicionar("val", corpo.resposta, {
      ferramentas: corpo.ferramentas,
      meta: `${corpo.versao_prompt} · ${segundos} s · ${corpo.tokens_entrada + corpo.tokens_saida} tokens${corpo.cortada ? " · resposta cortada" : ""}`,
    });
  } catch (erro) {
    digitando.remove();
    historico.pop(); // o turno que falhou não entra no histórico
    adicionar("erro", `Não consegui responder agora: ${erro.message}`);
  } finally {
    ocupado = false;
    enviar.disabled = false;
    entrada.focus();
  }
}

// ---------- bastidores: qual versão está no ar ----------
async function carregarInfo() {
  try {
    const r = await fetch("/api/info");
    const info = await r.json();
    const ambiente = $("#ambiente");
    const nomes = { production: "produção", staging: "staging", local: "local" };
    ambiente.textContent = nomes[info.ambiente] || info.ambiente;
    ambiente.classList.add(info.ambiente === "production" ? "producao" : info.ambiente);

    $("#info-versao").textContent = info.versao;
    const commit = (info.commit || "").slice(0, 7);
    if (info.repositorio && info.commit && info.commit !== "local") {
      $("#info-commit").innerHTML = `<a href="${escapar(info.repositorio)}/commit/${escapar(info.commit)}" target="_blank" rel="noopener">${escapar(commit)}</a>`;
    } else {
      $("#info-commit").textContent = commit || "—";
    }
    $("#info-build").textContent = info.data_build ? new Date(info.data_build).toLocaleString("pt-BR") : "—";
    $("#info-modelo").textContent = info.modelo;
    $("#info-prompt").textContent = info.prompt_canario
      ? `${info.prompt_estavel} · canário ${info.prompt_canario} (${info.pct_canario}%)`
      : info.prompt_estavel;

    status.textContent = info.chave_configurada ? "online" : "sem chave do modelo";
    status.className = `status ${info.chave_configurada ? "ok" : "falha"}`;
  } catch {
    status.textContent = "fora do ar";
    status.className = "status falha";
  }
}

// ---------- eventos ----------
form.addEventListener("submit", (e) => {
  e.preventDefault();
  const texto = entrada.value;
  entrada.value = "";
  entrada.style.height = "";
  perguntar(texto);
});

entrada.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

entrada.addEventListener("input", () => {
  entrada.style.height = "auto";
  entrada.style.height = `${Math.min(entrada.scrollHeight, 160)}px`;
});

document.querySelectorAll("[data-pergunta]").forEach((botao) =>
  botao.addEventListener("click", () => perguntar(botao.dataset.pergunta))
);

$("#nova").addEventListener("click", () => {
  historico = [];
  lista.innerHTML = '<li class="vazio" id="vazio"><p>Comece por uma das sugestões ao lado ou escreva sua dúvida.</p></li>';
  entrada.focus();
});

carregarInfo();
