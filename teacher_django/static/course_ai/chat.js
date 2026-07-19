(function(){
    function getJsonScriptText(id) {
        const element = document.getElementById(id);
        if (!element) return '';
        try {
            return JSON.parse(element.textContent);
        } catch (error) {
            return '';
        }
    }

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

    const input = document.getElementById('courseAiInput');
    const sendBtn = document.getElementById('courseAiSendBtn');
    const messages = document.getElementById('courseAiMessages');
    const status = document.getElementById('courseAiStatus');

    const context = {
        courseId: Number((window.__COURSE_AI_CONTEXT__ && window.__COURSE_AI_CONTEXT__.courseId) || 0),
        courseTitle: getJsonScriptText('course-ai-course-title'),
        materialId: Number(getJsonScriptText('course-ai-material-id') || 0),
        materialTitle: getJsonScriptText('course-ai-material-title'),
        currentPage: getJsonScriptText('course-ai-current-page') || '',
        planSummary: getJsonScriptText('course-ai-plan-summary') || null,
        focusSummary: getJsonScriptText('course-ai-focus-summary') || null,
        userAvatar: String((window.__COURSE_AI_CONTEXT__ && window.__COURSE_AI_CONTEXT__.userAvatar) || '').trim()
    };

    let conversationId = null;
    let requestInFlight = false;

    function setStatus(text) {
        if (status) status.textContent = text;
    }

    function escapeHtml(text) {
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function normalizeReferenceLabel(reference) {
        if (!reference) return '';
        const materialTitle = String(reference.material_title || '').trim();
        const heading = String(reference.heading || '').trim();
        const sourcePage = String(reference.source_page || '').trim();
        const parts = [];
        if (materialTitle) parts.push(materialTitle);
        if (heading) parts.push(heading);
        if (sourcePage) parts.push('第' + sourcePage + '页/张');
        return parts.join(' / ');
    }

    function buildReferenceHtml(reference) {
        if (!reference) return '';
        const label = normalizeReferenceLabel(reference);
        if (!label) return '';
        const pageValue = String(reference.source_page || '').trim();
        const materialId = String(reference.material_id || '').trim();
        const attrs = [];
        if (pageValue) attrs.push('data-reference-page="' + escapeHtml(pageValue) + '"');
        if (materialId) attrs.push('data-reference-material-id="' + escapeHtml(materialId) + '"');
        return '<button type="button" class="course-ai-reference" ' + attrs.join(' ') + '>' + escapeHtml(label) + '</button>';
    }

    function buildLearningPlanHtml(plan) {
        if (!plan || typeof plan !== 'object') return '';
        const tags = [];
        const topModuleName = String(plan.top_module_name || '').trim();
        const topModuleFocus = String(plan.top_module_focus || '').trim();
        const weakAreas = Array.isArray(plan.weak_areas) ? plan.weak_areas.filter(Boolean) : [];
        const reasons = Array.isArray(plan.recommendation_reason) ? plan.recommendation_reason.filter(Boolean) : [];
        if (topModuleName) tags.push('<span class="course-ai-reference">优先阶段：' + escapeHtml(topModuleName) + '</span>');
        if (weakAreas.length) tags.push('<span class="course-ai-reference">补弱点：' + escapeHtml(weakAreas.slice(0, 3).join('、')) + '</span>');
        if (!tags.length && !topModuleFocus && !reasons.length) return '';
        let html = '<div class="course-ai-evidence"><div class="course-ai-evidence-title">当前路径建议</div><div class="course-ai-reference-list">' + tags.join('') + '</div>';
        if (topModuleFocus) {
            html += '<div class="course-ai-guidance-card-copy">' + escapeHtml(topModuleFocus) + '</div>';
        }
        if (reasons.length) {
            html += '<div class="course-ai-guidance-card-copy">' + escapeHtml(reasons[0]) + '</div>';
        }
        html += '</div>';
        return html;
    }

    function buildStructuredAnswerHtml(text) {
        const source = String(text || '').replace(/\r\n/g, '\n').trim();
        if (!source) return '';
        const lines = source.split('\n');
        const sections = [];
        let current = null;

        function pushCurrent() {
            if (current && current.title) {
                current.body = current.body.join('\n').trim();
                sections.push(current);
            }
        }

        for (let i = 0; i < lines.length; i++) {
            const line = lines[i].trim();
            const match = line.match(/^([1-9]\d*)[\.、\)]\s*(.+)$/);
            if (match) {
                pushCurrent();
                current = { title: match[2].trim(), body: [] };
                continue;
            }
            if (!current) {
                continue;
            }
            current.body.push(line);
        }
        pushCurrent();

        if (!sections.length) {
            return escapeHtml(source).replace(/\n/g, '<br>');
        }

        let html = '<div class="course-ai-structured-response">';
        for (let i = 0; i < sections.length; i++) {
            const section = sections[i];
            html += '<div class="course-ai-response-section">';
            html += '<div class="course-ai-response-section-title">' + escapeHtml(section.title) + '</div>';
            html += '<div class="course-ai-response-section-body">' + escapeHtml(section.body || '').replace(/\n/g, '<br>') + '</div>';
            html += '</div>';
        }
        html += '</div>';
        return html;
    }

    function buildCourseEvidenceHtml(courseContext) {
        if (!courseContext || typeof courseContext !== 'object') return '';
        const references = Array.isArray(courseContext.references) ? courseContext.references : [];
        const referenceHtml = references
            .slice(0, 4)
            .map(buildReferenceHtml)
            .filter(Boolean)
            .join('');
        const learningPlanHtml = buildLearningPlanHtml(courseContext.learning_plan);
        const pageValue = String(courseContext.current_page || '').trim();
        const currentScopeBits = [];
        if (courseContext.material_title) currentScopeBits.push('当前资料：' + String(courseContext.material_title).trim());
        if (pageValue) currentScopeBits.push('当前页码：第' + pageValue + '页/张');
        let html = '';
        if (referenceHtml || currentScopeBits.length) {
            html += '<div class="course-ai-evidence"><div class="course-ai-evidence-title">资料依据</div>';
            if (currentScopeBits.length) {
                html += '<div class="course-ai-guidance-card-copy">' + escapeHtml(currentScopeBits.join('，')) + '</div>';
            }
            if (referenceHtml) {
                html += '<div class="course-ai-reference-list">' + referenceHtml + '</div>';
            }
            html += '</div>';
        }
        return html + learningPlanHtml;
    }

    // 多模态答疑：抽出 ```mermaid 图解代码块（单独渲染），正文按结构化渲染；公式($...$)交给 KaTeX
    function extractMermaid(text) {
        const srcs = [];
        const clean = String(text || '').replace(/```mermaid\s*([\s\S]*?)```/g, function(_, code) {
            srcs.push(code.trim()); return '';
        });
        return { clean: clean, mermaid: srcs };
    }

    function buildAssistantHtml(text, courseContext) {
        const ex = extractMermaid(text);
        const responseHtml = buildStructuredAnswerHtml(ex.clean);
        let mermaidHtml = '';
        ex.mermaid.forEach(function(src) {
            if (src) mermaidHtml += '<div class="mermaid course-ai-diagram">' + escapeHtml(src) + '</div>';
        });
        const evidenceHtml = buildCourseEvidenceHtml(courseContext);
        return '<div class="course-ai-bubble-inner">' + responseHtml + mermaidHtml + evidenceHtml + '</div>';
    }

    // 渲染答案里的图解(mermaid)与公式(KaTeX)
    function enhanceBubble(el) {
        if (!el) return;
        try {
            if (window.renderMathInElement) window.renderMathInElement(el, {
                delimiters: [
                    { left: '$$', right: '$$', display: true }, { left: '$', right: '$', display: false },
                    { left: '\\(', right: '\\)', display: false }, { left: '\\[', right: '\\]', display: true }
                ], throwOnError: false
            });
        } catch (e) {}
        const nodes = el.querySelectorAll('.mermaid');
        if (nodes.length && window.mermaid) { try { window.mermaid.run({ nodes: nodes, suppressErrors: true }); } catch (e) {} }
    }

    function appendMessage(role, text, courseContext) {
        const row = document.createElement('div');
        row.className = 'course-ai-message ' + role;
        
        const avatar = document.createElement('div');
        avatar.className = 'course-ai-avatar ' + role;
        if (role === 'user' && context.userAvatar) {
            avatar.classList.add('has-img');
            avatar.innerHTML = '<img src="' + context.userAvatar + '" alt="我" class="course-ai-avatar-img">';
        } else {
            avatar.innerHTML = role === 'user' ? '<i class="fas fa-user"></i>' : '<i class="fas fa-robot"></i>';
        }
        
        const bubble = document.createElement('div');
        bubble.className = 'course-ai-bubble';
        
        if (role === 'assistant') {
            bubble.innerHTML = buildAssistantHtml(text, courseContext);
        } else {
            bubble.innerHTML = escapeHtml(text).replace(/\n/g, '<br>');
        }
        
        row.appendChild(avatar);
        row.appendChild(bubble);
        messages.appendChild(row);
        if (role === 'assistant') enhanceBubble(bubble);
        scrollToBottom();
    }

    function scrollToBottom() {
        if (!messages) return;
        messages.scrollTop = messages.scrollHeight;
        window.requestAnimationFrame(function(){
            messages.scrollTop = messages.scrollHeight;
        });
    }

    function appendWelcome() {
        if (context.focusSummary && context.focusSummary.suggested_action) {
            appendMessage('assistant', '可以直接开始提问。当前建议：' + context.focusSummary.suggested_action, null);
            return;
        }
        if (context.planSummary && context.planSummary.top_module_name) {
            appendMessage('assistant', '可以直接开始提问。我会优先结合你最近调整后的学习路径来解释当前该先学什么、为什么先学这个。', null);
            return;
        }
        appendMessage('assistant', '你好！我是这门课程的 AI 助教。我可以帮你解答课程相关的问题，包括知识点解释、资料定位、学习建议等。有什么需要帮助的吗？', null);
    }

    function buildPersonalizedQuestion(query) {
        const trimmed = String(query || '').trim();
        if (!trimmed) return '';
        const prefixParts = [];
        if (context.focusSummary && context.focusSummary.top_module_name) {
            prefixParts.push('当前优先阶段：' + context.focusSummary.top_module_name);
        }
        if (context.focusSummary && Array.isArray(context.focusSummary.weak_areas) && context.focusSummary.weak_areas.length) {
            prefixParts.push('当前优先补弱：' + context.focusSummary.weak_areas.join('、'));
        }
        if (context.focusSummary && context.focusSummary.current_page) {
            prefixParts.push('当前页码：' + context.focusSummary.current_page);
        }
        if (!prefixParts.length) return trimmed;
        return '请结合以下当前学习状态来回答。' + prefixParts.join('；') + '。学生问题：' + trimmed;
    }

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

    function send() {
        const query = (input.value || '').trim();
        if (!query || requestInFlight) return;
        const personalizedQuery = buildPersonalizedQuestion(query);
        requestInFlight = true;
        setStatus('AI 正在思考...');
        
        if (sendBtn) {
            sendBtn.disabled = true;
            sendBtn.innerHTML = '<div class="course-ai-typing-indicator"><span class="course-ai-typing-dot"></span><span class="course-ai-typing-dot"></span><span class="course-ai-typing-dot"></span></div>';
        }
        
        appendMessage('user', query);
        input.value = '';

        function resetBtn(){
            requestInFlight = false;
            if (sendBtn) { sendBtn.disabled = false; sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i> 发送'; }
        }

        // 非流式回退（流式不可用/失败时用；skip_user_message 避免重复保存用户消息）
        function nonStreamSend(skipUserMessage){
            fetch('/agent/api/conversation/send/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
                credentials: 'same-origin',
                body: JSON.stringify({
                    text: personalizedQuery, conversation_id: conversationId, mode: 'ta',
                    course_id: context.courseId || null, material_id: context.materialId || null,
                    current_page: context.currentPage || null, skip_user_message: !!skipUserMessage
                })
            }).then(function(r){ return r.json(); }).then(function(data){
                resetBtn();
                if (!(data && data.ok)) { throw new Error((data && data.error) || '课程 AI 返回失败'); }
                conversationId = data.conversation_id || conversationId;
                appendMessage('assistant', data.assistant || '', data.course_context || null);
                showProfileToast(data.profile_update);
                setStatus('AI 已完成回答');
            }).catch(function(error){
                resetBtn(); setStatus('请求失败');
                appendMessage('assistant', '请求失败：' + (error.message || '未知错误'));
            });
        }

        if (typeof EventSource === 'undefined') { nonStreamSend(false); return; }

        // 优先流式：逐字渲染，结束时用最终文本 + 课程上下文做结构化渲染
        const params = new URLSearchParams();
        if (conversationId) params.set('conversation_id', conversationId);
        params.set('prompt', personalizedQuery);
        params.set('mode', 'ta');
        if (context.courseId) params.set('course_id', context.courseId);
        if (context.materialId) params.set('material_id', context.materialId);
        if (context.currentPage) params.set('current_page', context.currentPage);

        let es, bubbleEl = null, acc = '', gotContent = false, finished = false, courseCtx = null;
        try { es = new EventSource('/agent/api/conversation/stream/?' + params.toString(), { withCredentials: true }); }
        catch (e) { nonStreamSend(false); return; }

        function ensureBubble(){
            if (bubbleEl) return;
            const row = document.createElement('div'); row.className = 'course-ai-message assistant';
            const avatar = document.createElement('div'); avatar.className = 'course-ai-avatar assistant';
            avatar.innerHTML = '<i class="fas fa-robot"></i>';
            bubbleEl = document.createElement('div'); bubbleEl.className = 'course-ai-bubble';
            row.appendChild(avatar); row.appendChild(bubbleEl); messages.appendChild(row); scrollToBottom();
        }
        es.onmessage = function(ev){
            if (finished) return;
            gotContent = true; ensureBubble();
            acc += ev.data;
            bubbleEl.innerHTML = escapeHtml(acc).replace(/\n/g, '<br>');
            scrollToBottom();
        };
        es.addEventListener('done', function(ev){
            finished = true; es.close(); resetBtn(); setStatus('AI 已完成回答');
            let _pu = null;
            try { const p = JSON.parse(ev.data || '{}'); if (p.conversation_id) { conversationId = p.conversation_id; } courseCtx = p.course_context || null; _pu = p.profile_update; } catch(e){}
            // 即便没有任何流式分片也要落地气泡：否则用户看不到任何反馈
            ensureBubble();
            if (!acc.trim()) { acc = '（本次没有收到回复内容，请重试）'; }
            bubbleEl.innerHTML = buildAssistantHtml(acc, courseCtx); enhanceBubble(bubbleEl);
            showProfileToast(_pu);
        });
        es.addEventListener('failed', function(){
            if (finished) return;
            finished = true; es.close();
            if (!gotContent) { nonStreamSend(!!conversationId); }  // nonStreamSend 内部会 resetBtn
            // 有部分内容：收尾并恢复按钮（否则按钮永久卡在打字态）
            else { resetBtn(); setStatus('AI 已完成回答'); if (bubbleEl) { bubbleEl.innerHTML = buildAssistantHtml(acc, courseCtx); enhanceBubble(bubbleEl); } }
        });
        es.onerror = function(){
            if (finished) return;
            finished = true; try { es.close(); } catch(e){}
            if (!gotContent) { nonStreamSend(!!conversationId); }
            else { resetBtn(); setStatus('AI 已完成回答'); if (bubbleEl) { bubbleEl.innerHTML = buildAssistantHtml(acc, courseCtx); enhanceBubble(bubbleEl); } }
        };
    }

    function setCurrentPage(pageValue) {
        const normalized = String(pageValue || '').trim();
        const studyHelpers = window.__courseStudyHelpers || null;
        if (normalized && studyHelpers && typeof studyHelpers.setViewerPage === 'function') {
            studyHelpers.setViewerPage(normalized);
        }
    }

    function sendPrompt(promptText) {
        if (!input) return;
        input.value = String(promptText || '').trim();
        send();
    }

    if (sendBtn) {
        sendBtn.addEventListener('click', send);
    }
    if (input) {
        input.addEventListener('keydown', function(event){
            if (event.isComposing || event.keyCode === 229) return;
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                send();
            }
        });
    }
    if (messages) {
        messages.addEventListener('click', function(event){
            const reference = event.target.closest('[data-reference-page], [data-reference-material-id]');
            if (!reference) return;
            const pageValue = reference.getAttribute('data-reference-page') || '';
            const materialId = reference.getAttribute('data-reference-material-id') || '';
            const studyHelpers = window.__courseStudyHelpers || null;
            if (studyHelpers && materialId && typeof studyHelpers.goToMaterialPage === 'function' && String(materialId) !== String(studyHelpers.selectedMaterialId || '')) {
                studyHelpers.goToMaterialPage(materialId, pageValue);
                return;
            }
            if (pageValue) {
                setCurrentPage(pageValue);
            }
            if (typeof window.__setStudyMode === 'function') {
                window.__setStudyMode('asking');
            }
            const readingPanel = document.getElementById('studyReadingPanel');
            if (readingPanel) {
                readingPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });
    }

    document.querySelectorAll('[data-course-ai-prompt]').forEach(function(button){
        button.addEventListener('click', function(){
            const promptText = button.getAttribute('data-course-ai-prompt') || '';
            if (!promptText) return;
            sendPrompt(promptText);
        });
    });

    window.__courseAiHelpers = {
        setCurrentPage: setCurrentPage,
        sendPrompt: sendPrompt,
        scrollToBottom: scrollToBottom,
        focusInput: function() {
            if (input) input.focus();
        }
    };

    appendWelcome();
})();
