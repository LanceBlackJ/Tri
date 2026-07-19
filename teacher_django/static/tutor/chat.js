(function(){
    console.log('chat.js loaded');

    function getCookie(name) {
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    const csrftoken = getCookie('csrftoken');
    const chatWindow = document.getElementById('chatWindow');
    const input = document.getElementById('queryInput');
    const learningInput = document.getElementById('queryInputLearning');
    const peerInput = document.getElementById('queryInputPeer');
    const peerRecentTopicsInput = document.getElementById('peerRecentTopicsInput');
    const tutorPage = document.querySelector('.tutor-page');
    const sendBtn = document.getElementById('sendBtn');
    const conversationStatus = document.getElementById('conversationStatus');
    const conversationList = document.getElementById('conversationList');
    const newConversationBtn = document.getElementById('newConversationBtn');
    const modeDescription = document.getElementById('modeDescription');
    const pageParams = new URLSearchParams(window.location.search || '');
    const tutorContext = {
        courseId: pageParams.get('course_id') || '',
        materialId: pageParams.get('material_id') || '',
        courseTitle: pageParams.get('course_title') || '',
        materialTitle: pageParams.get('material_title') || '',
        currentPage: pageParams.get('current_page') || '',
        courseMapSummary: ''
    };

    const CHAT_MODE_WELCOME = '你好！我是你的AI助手。有什么问题想问我吗？';
    const LEARNING_MODE_WELCOME = '你好！我是你的学习助手。告诉我你想学什么，或者有什么困惑，我会根据你的情况为你提供个性化的学习建议。';

    let conversationId = null;
    let requestInFlight = false;
    let cachedConversations = [];
    let peerSessionEnded = false;  // 费曼互教是否已收尾（小艾判断学生已讲清楚）——收尾后禁用输入

    function setConversationStatus(text){
        if(conversationStatus){
            conversationStatus.textContent = text;
        }
    }

    function getCurrentMode() {
        const activeBtn = document.querySelector('.tutor-mode-btn.is-active');
        return activeBtn ? activeBtn.dataset.mode : 'chat';
    }

    function updateModeDescription() {
        if (!modeDescription) return;
        const mode = getCurrentMode();
        if (mode === 'chat') {
            modeDescription.textContent = '智能对话，随时为你解答';
        } else if (mode === 'learning') {
            modeDescription.textContent = '个性化学习助手，因材施教';
        } else {
            modeDescription.textContent = '和AI同学互相讲题，巩固理解';
        }
    }

    function escapeHtml(text){
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatInlineMarkup(text){
        let html = escapeHtml(text);
        // 粗体 **text** 或 __text__
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/__([^_]+)__/g, '<strong>$1</strong>');
        // 斜体 *text* 或 _text_
        html = html.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
        html = html.replace(/(?<!_)_([^_]+)_(?!_)/g, '<em>$1</em>');
        // 行内代码 `code`
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        // 链接 [text](url)
        html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
        // 删除线 ~~text~~
        html = html.replace(/~~([^~]+)~~/g, '<del>$1</del>');
        return html;
    }

    function formatTextBlock(block){
        const trimmed = block.trim();
        if(!trimmed){
            return '';
        }

        const lines = trimmed.split('\n');
        
        // 引用块 > text
        const isQuote = lines.every(function(line){
            return /^\s*&gt;\s/.test(line) || /^\s*>\s/.test(line);
        });
        if(isQuote){
            const quoteLines = lines.map(function(line){
                return line.replace(/^\s*&gt;\s?/, '').replace(/^\s*>\s?/, '');
            });
            return '<blockquote>' + quoteLines.map(function(line){
                return formatInlineMarkup(line);
            }).join('<br/>') + '</blockquote>';
        }

        const unorderedList = lines.every(function(line){
            return /^\s*[-*]\s+/.test(line);
        });
        const orderedList = lines.every(function(line){
            return /^\s*\d+\.\s+/.test(line);
        });

        if(unorderedList){
            return '<ul>' + lines.map(function(line){
                return '<li>' + formatInlineMarkup(line.replace(/^\s*[-*]\s+/, '')) + '</li>';
            }).join('') + '</ul>';
        }

        if(orderedList){
            return '<ol>' + lines.map(function(line){
                return '<li>' + formatInlineMarkup(line.replace(/^\s*\d+\.\s+/, '')) + '</li>';
            }).join('') + '</ol>';
        }

        return '<p>' + lines.map(function(line){
            return formatInlineMarkup(line);
        }).join('<br/>') + '</p>';
    }

    // 保护 LaTeX 公式：在文本格式化之前把公式替换成占位符，避免 markdown 把
    // 公式里的 _ * \ 当成强调语法吃掉；格式化完再还原，交给 KaTeX 渲染。
    function protectMath(str){
        const store = [];
        function grab(re){
            str = str.replace(re, function(m){ store.push(m); return 'KTXMATH' + (store.length - 1) + 'KTXEND'; });
        }
        grab(/\$\$[\s\S]+?\$\$/g);          // 块级 $$...$$
        grab(/\\\[[\s\S]+?\\\]/g);          // 块级 \[...\]
        grab(/\\\([\s\S]+?\\\)/g);          // 行内 \(...\)
        grab(/\$(?!\s)(?:\\.|[^$\\\n])+?\$/g); // 行内 $...$
        return { text: str, store: store };
    }
    function restoreMath(html, store){
        return html.replace(/KTXMATH(\d+)KTXEND/g, function(_, i){ return store[+i]; });
    }

    function formatRichText(text){
        const source = String(text || '').replace(/\r\n/g, '\n');
        const parts = [];
        const fencePattern = /```([a-zA-Z0-9_-]+)?\n([\s\S]*?)```/g;
        let lastIndex = 0;
        let match;

        while((match = fencePattern.exec(source)) !== null){
            if(match.index > lastIndex){
                parts.push({type: 'text', value: source.slice(lastIndex, match.index)});
            }
            parts.push({type: 'code', lang: match[1] || '', value: match[2] || ''});
            lastIndex = fencePattern.lastIndex;
        }

        if(lastIndex < source.length){
            parts.push({type: 'text', value: source.slice(lastIndex)});
        }

        if(!parts.length){
            parts.push({type: 'text', value: source});
        }

        return '<div class="tutor-rich-content">' + parts.map(function(part){
            if(part.type === 'code'){
                const languageClass = part.lang ? ' language-' + escapeHtml(part.lang) : '';
                return '<pre class="tutor-code-block"><code class="' + languageClass.trim() + '">' + escapeHtml(part.value).replace(/\n$/, '') + '</code></pre>';
            }
            // 先保护公式，再做文本/markdown 格式化，最后还原公式（交给 KaTeX 渲染）
            const mp = protectMath(part.value);
            const formatted = mp.text
                .split(/\n{2,}/)
                .map(formatTextBlock)
                .filter(Boolean)
                .join('');
            return restoreMath(formatted, mp.store);
        }).join('') + '</div>';
    }

    function renderMathContent(element){
        if(!element || typeof window.renderMathInElement !== 'function') return;
        window.renderMathInElement(element, {
            delimiters: [
                {left: '$$', right: '$$', display: true},
                {left: '\\[', right: '\\]', display: true},
                {left: '$', right: '$', display: false},
                {left: '\\(', right: '\\)', display: false}
            ],
            throwOnError: false,
            strict: 'ignore'
        });
    }

    function highlightCodeBlocks(element){
        if(!element || typeof hljs === 'undefined') return;
        const blocks = element.querySelectorAll('pre code');
        blocks.forEach(function(block){
            if(block.classList.contains('language-mermaid') || block.classList.contains('lang-mermaid')) return; // 图解交给 mermaid
            if(!block.classList.contains('hljs')){
                hljs.highlightElement(block);
            }
        });
    }

    // 多模态答疑：把 ```mermaid 代码块渲染成流程图/概念图（图解说明）
    function renderMermaidIn(element){
        if(!element || typeof window.mermaid === 'undefined') return;
        const codes = element.querySelectorAll('code.language-mermaid, code.lang-mermaid');
        codes.forEach(function(code){
            const src = (code.textContent || '').trim();
            if(!src) return;
            const holder = document.createElement('div');
            holder.className = 'mermaid';
            holder.textContent = src;
            const pre = code.closest('pre') || code;
            if(pre.parentNode) pre.parentNode.replaceChild(holder, pre);
        });
        const nodes = element.querySelectorAll('.mermaid');
        if(nodes.length){
            try { window.mermaid.run({ nodes: nodes, suppressErrors: true }); } catch(e){}
        }
    }

    // 用户本人上传的头像（没有则回退到"你"字）
    const USER_AVATAR_URL = (typeof window.__TUTOR_USER_AVATAR__ === 'string' ? window.__TUTOR_USER_AVATAR__ : '').trim();
    function userAvatarInner(){
        return USER_AVATAR_URL ? '<img src="' + USER_AVATAR_URL + '" alt="我" class="tutor-avatar-img">' : '你';
    }

    function appendMessage(role, text){
        const el = document.createElement('div');
        const messageHtml = formatRichText(text);
        el.className = 'tutor-chat-row ' + (role === 'user' ? 'text-right mb-3' : 'text-left mb-3');
        el.innerHTML = `
            <div class="tutor-chat-stack ${role==='user' ? 'is-user' : ''}">
                <div class="tutor-message-avatar ${role==='user' ? ('is-user' + (USER_AVATAR_URL ? ' has-img' : '')) : 'is-assistant'}">${role==='user' ? userAvatarInner() : 'AI'}</div>
                <div class="tutor-message-column ${role==='user' ? 'is-user' : 'is-assistant'}">
                    <div class="tutor-message-bubble ${role==='user'? 'is-user':'is-assistant'}">${messageHtml}</div>
                </div>
            </div>
        `;
        chatWindow.appendChild(el);
        renderMermaidIn(el.querySelector('.tutor-message-bubble'));
        renderMathContent(el.querySelector('.tutor-message-bubble'));
        highlightCodeBlocks(el.querySelector('.tutor-message-bubble'));
        chatWindow.scrollTop = chatWindow.scrollHeight;
        return el;
    }

    function appendPendingMessage(){
        const el = document.createElement('div');
        el.className = 'tutor-chat-row text-left mb-3';
        el.innerHTML = `
            <div class="tutor-chat-stack">
                <div class="tutor-message-avatar is-assistant">AI</div>
                <div class="tutor-message-bubble is-assistant is-pending">
                    <div class="tutor-typing-dots"><span></span><span></span><span></span></div>
                </div>
            </div>
        `;
        chatWindow.appendChild(el);
        chatWindow.scrollTop = chatWindow.scrollHeight;
        return el;
    }

    function clearMessages(){
        chatWindow.innerHTML = '';
    }

    function upsertConversationSummary(item){
        if(!item || !item.id) return;
        const nextItems = cachedConversations.filter(function(existing){
            return String(existing.id) !== String(item.id);
        });
        nextItems.unshift(item);
        cachedConversations = nextItems;
        renderConversationList(cachedConversations, item.id);
    }

    function resetToNewConversationState(){
        conversationId = null;
        clearMessages();
        input.value = '';
        if (learningInput) learningInput.value = '';
        if (peerInput) peerInput.value = '';
        renderConversationList(cachedConversations, null);
        setConversationStatus('已开启新一轮对话');
        showWelcome();
    }

    function deleteConversation(targetConversationId){
        if(!targetConversationId || requestInFlight) return;
        if(!window.confirm('确认删除这个对话吗？删除后无法恢复。')) return;

        fetch('/agent/api/conversation/delete/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrftoken
            },
            credentials: 'same-origin',
            body: JSON.stringify({conversation_id: targetConversationId})
        }).then(function(response){
            return response.json();
        }).then(function(data){
            if(!(data && data.ok)){
                throw new Error((data && data.error) || '删除失败');
            }

            cachedConversations = cachedConversations.filter(function(item){
                return String(item.id) !== String(targetConversationId);
            });

            if(String(conversationId) === String(targetConversationId)){
                resetToNewConversationState();
            } else {
                renderConversationList(cachedConversations, conversationId);
            }
        }).catch(function(err){
            setConversationStatus(err.message || '删除失败，请稍后重试');
        });
    }

    function renderConversationList(items, selectedId){
        if(!conversationList) return;
        cachedConversations = Array.isArray(items) ? items.slice() : [];
        conversationList.innerHTML = '';
        (items || []).forEach(function(item){
            const row = document.createElement('div');
            row.className = 'tutor-conversation-item';
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'tutor-conversation-main text-left rounded-2xl border px-4 py-3 transition-all duration-200 ' + (String(item.id) === String(selectedId) ? 'border-blue-500 bg-blue-50 shadow-sm' : 'border-slate-200 bg-white/90 hover:border-blue-300 hover:bg-blue-50/50');
            btn.innerHTML = `<div class="font-semibold text-sm text-slate-900">${item.title}</div><div class="text-xs text-slate-500 mt-1">${item.updated_at}</div>`;
            btn.addEventListener('click', function(){
                loadHistory(item.id);
            });

            const deleteBtn = document.createElement('button');
            deleteBtn.type = 'button';
            deleteBtn.className = 'tutor-conversation-delete';
            deleteBtn.setAttribute('aria-label', '删除对话');
            deleteBtn.title = '删除对话';
            deleteBtn.textContent = '×';
            deleteBtn.addEventListener('click', function(event){
                event.preventDefault();
                event.stopPropagation();
                deleteConversation(item.id);
            });

            row.appendChild(btn);
            row.appendChild(deleteBtn);
            conversationList.appendChild(row);
        });
    }

    function renderHistoryMessages(messages){
        clearMessages();
        if(!messages || !messages.length){
            showWelcome();
            return;
        }
        messages.forEach(function(message){
            appendMessage(
                message.role === 'student' ? 'user' : 'assistant',
                message.content || ''
            );
        });
    }

    function loadHistory(targetConversationId){
        const query = targetConversationId ? ('?conversation_id=' + encodeURIComponent(targetConversationId)) : '';
        fetch('/agent/api/conversation/history/' + query, {
            method: 'GET',
            credentials: 'same-origin'
        }).then(function(response){
            return response.json();
        }).then(function(data){
            if(!(data && data.ok)) return;
            conversationId = data.selected_conversation_id || null;
            renderConversationList(data.conversations, data.selected_conversation_id);
            // 跳回这条历史记录所属的对话模式（后端按消息 persona 推断）
            const selItem = (data.conversations || []).find(function(c){ return String(c.id) === String(data.selected_conversation_id); });
            const convMode = data.mode || (selItem && selItem.mode) || '';
            if (convMode && convMode !== getCurrentMode() && ['chat','learning','peer_teaching'].indexOf(convMode) !== -1) {
                applyModeUI(convMode);
            }
            // 载入历史前先解除上一轮可能残留的收尾禁用
            peerSessionEnded = false;
            if (sendBtn) sendBtn.disabled = false;
            if (peerInput) peerInput.disabled = false;
            renderHistoryMessages(data.messages);
            // 若载入的是一段"已收尾"的费曼互教，恢复成已完成状态（禁用输入 + 再来一轮）
            if (convMode === 'peer_teaching') {
                const msgs = data.messages || [];
                const lastAsst = msgs.slice().reverse().find(function(m){ return m.role === 'assistant'; });
                if (lastAsst && lastAsst.peer_complete) { finishPeerSession(); }
            }
            if(!data.selected_conversation_id){
                showWelcome();
            }
        }).catch(function(err){
            setConversationStatus('历史会话加载失败');
            showWelcome();
        });
    }

    function showWelcome(){
        clearMessages();
        const mode = getCurrentMode();
        if (mode === 'peer_teaching') { showPeerSetup(); return; }
        const welcomeText = (mode === 'learning') ? LEARNING_MODE_WELCOME : CHAT_MODE_WELCOME;
        appendMessage('assistant', welcomeText);
        setConversationStatus('已准备好开始');
    }

    // 费曼互教：进入模式先让用户设定一个"最近学过的主题"，随后小艾主动开口向用户请教
    function showPeerSetup(){
        clearMessages();
        conversationId = null;
        peerSessionEnded = false;                       // 新一轮：解除上一轮的收尾禁用
        if (sendBtn) sendBtn.disabled = false;
        if (peerInput) peerInput.disabled = false;
        if (peerRecentTopicsInput) peerRecentTopicsInput.value = '';
        const el = document.createElement('div');
        el.className = 'tutor-peer-setup';
        el.innerHTML =
            '<div class="tutor-peer-setup-card">'
          +   '<div class="tutor-peer-setup-title">🧑‍🤝‍🧑 教会小艾</div>'
          +   '<div class="tutor-peer-setup-desc">告诉小艾你最近学了哪个主题，它会像一个还没完全学懂的同学，主动来向你请教、追问。你在把它讲清楚的过程中，正好巩固自己的理解。</div>'
          +   '<input type="text" id="peerTopicInput" class="tutor-peer-setup-input" placeholder="例如：梯度下降、二叉搜索树、PPO 算法…" />'
          +   '<button type="button" id="peerStartBtn" class="tutor-peer-setup-btn">开始互教 · 让小艾来问我</button>'
          +   '</div>';
        chatWindow.appendChild(el);
        const topicInput = el.querySelector('#peerTopicInput');
        const startBtn = el.querySelector('#peerStartBtn');
        if (topicInput) {
            topicInput.focus();
            topicInput.addEventListener('keydown', function(e){
                if (e.isComposing || e.keyCode === 229) return;
                if (e.key === 'Enter') { e.preventDefault(); startPeerTeaching(topicInput.value); }
            });
        }
        if (startBtn) startBtn.addEventListener('click', function(){ startPeerTeaching(topicInput ? topicInput.value : ''); });
        setConversationStatus('设定主题后，小艾会来向你请教');
    }

    function startPeerTeaching(topic){
        const t = String(topic || '').trim();
        if (!t) {
            const inp = document.getElementById('peerTopicInput');
            if (inp) inp.focus();
            setConversationStatus('先填一个你最近学过的主题');
            return;
        }
        if (requestInFlight) return;
        if (peerRecentTopicsInput) peerRecentTopicsInput.value = t;  // 供后续每条讲解带上 recent_topics
        clearMessages();
        const chip = document.createElement('div');
        chip.className = 'tutor-peer-topic-chip';
        chip.innerHTML = '📖 本次互教主题：<strong>' + escapeHtml(t) + '</strong>';
        chatWindow.appendChild(chip);

        requestInFlight = true;
        if (sendBtn) sendBtn.disabled = true;
        setConversationStatus('小艾正在想第一个问题…');
        const loading = appendPendingMessage();
        fetch('/agent/api/conversation/send/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrftoken },
            credentials: 'same-origin',
            body: JSON.stringify({
                text: t, mode: 'peer_teaching', recent_topics: t, kickoff: true, skip_user_message: true,
                course_id: tutorContext.courseId || null, material_id: tutorContext.materialId || null,
                current_page: tutorContext.currentPage || null
            })
        }).then(function(r){ return r.json(); }).then(function(data){
            if (loading) loading.remove();
            if (data && data.ok) {
                if (data.conversation_id) { conversationId = data.conversation_id; }
                if (data.conversation) { upsertConversationSummary(data.conversation); }
                appendMessage('assistant', data.assistant || '');
                setConversationStatus('轮到你给小艾讲解了');
                const pi = getActiveInput('peer_teaching');
                if (pi) pi.focus();
            } else {
                appendMessage('assistant', '开场失败：' + ((data && data.error) || '未知错误'));
                setConversationStatus('开场失败');
            }
        }).catch(function(err){
            if (loading) loading.remove();
            appendMessage('assistant', '开场失败：' + err.message);
            setConversationStatus('请求失败');
        }).finally(function(){
            requestInFlight = false;
            if (sendBtn) sendBtn.disabled = false;
        });
    }

    // 费曼互教收尾：小艾判断学生已讲清楚 → 结束本轮，禁用输入并给出"再来一轮"入口
    function finishPeerSession(){
        peerSessionEnded = true;
        if (peerInput) peerInput.disabled = true;
        if (sendBtn) sendBtn.disabled = true;
        const el = document.createElement('div');
        el.className = 'tutor-peer-done';
        el.innerHTML =
            '<div class="tutor-peer-done-card">'
          +   '<div class="tutor-peer-done-title">🎉 本次互教完成</div>'
          +   '<div class="tutor-peer-done-desc">小艾说它听懂了——你在把它讲清楚的过程中，也巩固了这个知识点。</div>'
          +   '<button type="button" id="peerRestartBtn" class="tutor-peer-done-btn">再来一轮 · 换个主题</button>'
          +   '</div>';
        chatWindow.appendChild(el);
        chatWindow.scrollTop = chatWindow.scrollHeight;
        setConversationStatus('本次互教已完成');
        const rb = el.querySelector('#peerRestartBtn');
        if (rb) rb.addEventListener('click', function(){ showPeerSetup(); });
    }

    function getActiveInput(mode){
        if (mode === 'learning' && learningInput) return learningInput;
        if (mode === 'peer_teaching' && peerInput) return peerInput;
        return input;
    }

    // "画像已更新"轻提示：让"随学随新"在聊天时可见
    function showProfileToast(update){
        if (!update || !update.updated) return;
        const dims = (update.dimensions || []).slice(0, 4).join('、');
        const t = document.createElement('div');
        t.style.cssText = 'position:fixed;right:20px;bottom:20px;z-index:9999;background:linear-gradient(135deg,#12568a,#38bdf8);color:#fff;padding:10px 16px;border-radius:10px;box-shadow:0 8px 24px rgba(2,6,23,.25);font-size:13px;opacity:0;transform:translateY(8px);transition:all .3s;';
        t.innerHTML = '<i class="fas fa-brain"></i> 学习画像已更新' + (dims ? '：' + dims : '');
        document.body.appendChild(t);
        requestAnimationFrame(function(){ t.style.opacity = '1'; t.style.transform = 'translateY(0)'; });
        setTimeout(function(){ t.style.opacity = '0'; t.style.transform = 'translateY(8px)'; setTimeout(function(){ t.remove(); }, 300); }, 3200);
    }

    function send(forcedText){
        const currentMode = getCurrentMode();
        const activeInput = getActiveInput(currentMode);
        const query = (forcedText || activeInput.value || '').trim();
        if(!query || requestInFlight) return;

        appendMessage('user', query);
        activeInput.value = '';
        if (learningInput && currentMode !== 'learning') {
            learningInput.value = '';
        }

        let statusText = 'AI正在思考...';
        if (currentMode === 'learning') {
            statusText = '学习助手正在分析你的需求...';
        } else if (currentMode === 'peer_teaching') {
            statusText = '小艾正在思考...';
        }
        // 发送中：置忙 + 禁用按钮，防止并发发送（多个流交错、conversationId 互相覆盖）
        requestInFlight = true;
        if (sendBtn) sendBtn.disabled = true;
        function finishSend(){ requestInFlight = false; if (sendBtn) sendBtn.disabled = peerSessionEnded; }

        setConversationStatus(statusText);
        const loading = appendPendingMessage();
        const recentTopics = (currentMode === 'peer_teaching' && peerRecentTopicsInput)
            ? (peerRecentTopicsInput.value || '').trim() : '';

        // 非流式回退（流式不可用/失败时用；带 skip_user_message 避免重复保存用户消息）
        function nonStreamSend(loadingEl, skipUserMessage){
            fetch('/agent/api/conversation/send/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrftoken },
                credentials: 'same-origin',
                body: JSON.stringify({
                    text: query, conversation_id: conversationId,
                    course_id: tutorContext.courseId || null,
                    material_id: tutorContext.materialId || null,
                    current_page: tutorContext.currentPage || null,
                    mode: currentMode, recent_topics: recentTopics,
                    skip_user_message: !!skipUserMessage
                })
            }).then(function(r){ return r.json(); }).then(function(data){
                setConversationStatus('对话已更新');
                if (loadingEl) loadingEl.remove();
                if(data && data.ok){
                    appendMessage('assistant', data.assistant || data.message || '');
                    if(data.conversation_id){ conversationId = data.conversation_id; }
                    if(data.conversation){ upsertConversationSummary(data.conversation); }
                    if(data.knowledge_tracing && currentMode === 'learning'){ updateKnowledgeTracingPanel(data.knowledge_tracing); }
                    showProfileToast(data.profile_update);
                    // 费曼互教：小艾判断学生已讲清楚 → 收尾结束本轮
                    if(data.peer_complete && currentMode === 'peer_teaching'){ finishPeerSession(); }
                } else {
                    setConversationStatus('返回了错误');
                    appendMessage('assistant', '错误：' + (data.error || '未知错误'));
                }
            }).catch(function(err){
                setConversationStatus('请求失败，请稍后重试');
                if (loadingEl) loadingEl.remove();
                appendMessage('assistant', '请求失败：' + err.message);
            }).finally(function(){ finishSend(); });
        }

        // 学习 / 费曼互教模式：只走非流式 send —— 只有它会跑 USER-LLM R1 / 知识追踪 /
        // 自我解释 / 小艾评估 / recent_topics 等模式特化逻辑（流式端点没有这些）。
        // chat 模式无特化逻辑，保留逐字流式体验。
        if (currentMode === 'learning' || currentMode === 'peer_teaching') { nonStreamSend(loading, false); return; }

        // 优先流式：逐字渲染；不支持/失败则回退非流式
        if (typeof EventSource === 'undefined') { nonStreamSend(loading, false); return; }

        const params = new URLSearchParams();
        if (conversationId) params.set('conversation_id', conversationId);
        params.set('prompt', query);
        params.set('mode', currentMode);
        if (tutorContext.courseId) params.set('course_id', tutorContext.courseId);
        if (tutorContext.materialId) params.set('material_id', tutorContext.materialId);
        if (tutorContext.currentPage) params.set('current_page', tutorContext.currentPage);

        let es, bubbleRow = null, bubbleEl = null, acc = '', gotContent = false, finished = false;
        try { es = new EventSource('/agent/api/conversation/stream/?' + params.toString(), { withCredentials: true }); }
        catch (e) { nonStreamSend(loading, false); return; }

        function ensureBubble(){
            if (bubbleRow) return;
            if (loading) loading.remove();
            bubbleRow = appendMessage('assistant', '');
            bubbleEl = bubbleRow.querySelector('.tutor-message-bubble');
        }
        function renderAcc(){
            if (!bubbleEl) return;
            bubbleEl.innerHTML = formatRichText(acc);
            renderMathContent(bubbleEl); highlightCodeBlocks(bubbleEl);
            chatWindow.scrollTop = chatWindow.scrollHeight;
        }

        es.onmessage = function(ev){
            if (finished) return;
            gotContent = true;
            ensureBubble();
            acc += ev.data;
            renderAcc();
        };
        es.addEventListener('done', function(ev){
            finished = true; es.close();
            setConversationStatus('对话已更新');
            try {
                const payload = JSON.parse(ev.data || '{}');
                if(payload.conversation_id){ conversationId = payload.conversation_id; }
                if(payload.conversation){ upsertConversationSummary(payload.conversation); }
                if(payload.knowledge_tracing && currentMode === 'learning'){ updateKnowledgeTracingPanel(payload.knowledge_tracing); }
                showProfileToast(payload.profile_update);
            } catch(e){}
            // 即便没有任何流式分片也要落地气泡：否则"打字中"loading 会永久残留、且用户看不到任何反馈
            ensureBubble();
            if (!acc.trim()) { acc = '（本次没有收到回复内容，请重试）'; }
            renderAcc();
            if (bubbleEl) renderMermaidIn(bubbleEl);  // 流式结束后再渲染图解（半截 mermaid 会失败）
            finishSend();
        });
        es.addEventListener('failed', function(){
            // 服务端明确报错（如流式不可用）：无内容则回退非流式；有部分内容则收尾
            if (finished) return;
            finished = true; es.close();
            if (!gotContent) { nonStreamSend(loading, !!conversationId); }  // nonStreamSend 内部会 finishSend
            else { renderAcc(); if (bubbleEl) renderMermaidIn(bubbleEl); finishSend(); }
        });
        es.onerror = function(){
            if (finished) return;
            finished = true;
            try { es.close(); } catch(e){}
            if (!gotContent) { nonStreamSend(loading, !!conversationId); }
            else { setConversationStatus('对话已更新'); renderAcc(); if (bubbleEl) renderMermaidIn(bubbleEl); finishSend(); }
        };
    }

    if(sendBtn){
        sendBtn.addEventListener('click', send);
    }

    function handleInputKeydown(e){
        if(e.isComposing || e.keyCode === 229){
            return;
        }
        if(e.key === 'Enter' && !e.shiftKey){
            e.preventDefault();
            send();
        }
    }

    if(input){
        input.addEventListener('keydown', handleInputKeydown);
    }
    if(learningInput){
        learningInput.addEventListener('keydown', handleInputKeydown);
    }
    if(peerInput){
        peerInput.addEventListener('keydown', handleInputKeydown);
    }

    if(newConversationBtn){
        newConversationBtn.addEventListener('click', function(){
            if(requestInFlight) return;
            resetToNewConversationState();
        });
    }

    // AKT知识追踪面板更新函数
    function updateKnowledgeTracingPanel(ktData){
        const panel = document.getElementById('knowledgeTracingPanel');
        if(!panel || !ktData) return;
        
        // 显示面板
        panel.style.display = 'block';
        
        // 更新徽章
        const badge = document.getElementById('ktMasteryBadge');
        if(badge){
            const masteryPercent = Math.round((ktData.average_mastery || 0) * 100);
            badge.textContent = masteryPercent + '%';
        }
        
        // 更新统计数据
        const mastered = document.getElementById('ktMastered');
        const learning = document.getElementById('ktLearning');
        const newConcepts = document.getElementById('ktNew');
        
        if(mastered) mastered.textContent = ktData.mastered_concepts || 0;
        if(learning) learning.textContent = ktData.learning_concepts || 0;
        if(newConcepts) newConcepts.textContent = ktData.new_concepts || 0;
        
        // 更新知识点列表
        const conceptsContainer = document.getElementById('ktConcepts');
        if(!conceptsContainer || !ktData.concepts) return;
        
        conceptsContainer.innerHTML = '';
        
        // 按掌握度排序，显示最近的知识点
        const sortedConcepts = (ktData.concepts || [])
            .slice()
            .sort(function(a, b){ return b.mastery_probability - a.mastery_probability; })
            .slice(0, 8);  // 最多显示8个
        
        sortedConcepts.forEach(function(concept){
            const masteryPercent = Math.round((concept.mastery_probability || 0) * 100);
            let statusClass = 'tutor-kt-status-new';
            let barColor = '#64748b';
            
            if(concept.mastery_probability >= 0.8){
                statusClass = 'tutor-kt-status-mastered';
                barColor = '#12568a';
            } else if(concept.mastery_probability >= 0.3){
                statusClass = 'tutor-kt-status-learning';
                barColor = '#3b82f6';
            }
            
            const conceptEl = document.createElement('div');
            conceptEl.className = 'tutor-kt-concept';
            conceptEl.innerHTML = 
                '<span class="tutor-kt-concept-name ' + statusClass + '">' + concept.name + '</span>' +
                '<div class="tutor-kt-concept-bar">' +
                    '<div class="tutor-kt-concept-fill" style="width: ' + masteryPercent + '%; background: ' + barColor + ';"></div>' +
                '</div>' +
                '<span class="tutor-kt-concept-mastery ' + statusClass + '">' + masteryPercent + '%</span>';
            
            conceptsContainer.appendChild(conceptEl);
        });
    }

    // 模式切换功能
    const modeSwitchSlider = document.querySelector('.tutor-mode-switch-slider');
    const modeBtns = document.querySelectorAll('.tutor-mode-btn');

    // 只切换模式的界面状态（高亮按钮/滑块/页面类名/描述），不清空窗口、不重置会话。
    // 供两处复用：switchMode(点模式按钮，开新对话) 和 loadHistory(点历史记录，跳回该会话所属模式)。
    function applyModeUI(mode) {
        if (!tutorPage) return;
        tutorPage.classList.remove('is-chat-mode', 'is-learning-mode', 'is-peer-mode');
        if (mode === 'chat') {
            tutorPage.classList.add('is-chat-mode');
        } else if (mode === 'learning') {
            tutorPage.classList.add('is-learning-mode');
        } else if (mode === 'peer_teaching') {
            tutorPage.classList.add('is-peer-mode');
        }
        if (modeSwitchSlider) {
            modeSwitchSlider.classList.remove('is-learning', 'is-peer');
            if (mode === 'learning') {
                modeSwitchSlider.classList.add('is-learning');
            } else if (mode === 'peer_teaching') {
                modeSwitchSlider.classList.add('is-peer');
            }
        }
        modeBtns.forEach(function(btn) {
            if (btn.dataset.mode === mode) {
                btn.classList.add('is-active');
            } else {
                btn.classList.remove('is-active');
            }
        });
        localStorage.setItem('tutorMode', mode);
        updateModeDescription();
    }

    function switchMode(mode) {
        if (!tutorPage) return;
        applyModeUI(mode);

        // 清空当前聊天窗口，重置会话——点模式按钮=开一段新对话
        if (chatWindow) { chatWindow.innerHTML = ''; }
        conversationId = null;
        peerSessionEnded = false;                       // 解除可能残留的收尾禁用
        if (sendBtn) sendBtn.disabled = false;
        if (peerInput) peerInput.disabled = false;

        // 同步输入框内容
        if (mode === 'chat' && learningInput && input) {
            input.value = learningInput.value;
        } else if (mode === 'learning' && learningInput && input) {
            learningInput.value = input.value;
        }

        showWelcome();
    }

    modeBtns.forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            // 请求进行中禁止切模式：否则会清空 conversationId/窗口，
            // 而在途的旧模式回调仍会 appendMessage 并回写 conversationId，把回答塞进新模式、污染会话
            if (requestInFlight) {
                setConversationStatus('请等当前回答完成后再切换模式');
                return;
            }
            const mode = btn.dataset.mode;
            switchMode(mode);
        });
    });

    const savedMode = localStorage.getItem('tutorMode');
    if (savedMode) {
        switchMode(savedMode);
    }

    if (peerRecentTopicsInput) {
        const savedRecentTopics = localStorage.getItem('peerRecentTopics');
        if (savedRecentTopics) {
            peerRecentTopicsInput.value = savedRecentTopics;
        }
        peerRecentTopicsInput.addEventListener('input', function() {
            localStorage.setItem('peerRecentTopics', peerRecentTopicsInput.value || '');
        });
    }

    if (input && learningInput) {
        input.addEventListener('input', function() {
            if (tutorPage && tutorPage.classList.contains('is-learning-mode')) {
                learningInput.value = input.value;
            }
        });
        learningInput.addEventListener('input', function() {
            if (tutorPage && tutorPage.classList.contains('is-chat-mode')) {
                input.value = learningInput.value;
            }
        });
    }

    loadHistory();
})();