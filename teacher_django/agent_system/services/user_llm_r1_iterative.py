"""
USER-LLM R1 完整实现：迭代推理引擎
实现多次迭代优化的推理机制
"""
import json
import logging
from typing import Dict, List, Tuple
from datetime import datetime

from django.contrib.auth import get_user_model

from .xinghuo_client import XinghuoClient

logger = logging.getLogger(__name__)
User = get_user_model()


class IterationConfig:
    """迭代配置"""
    MAX_ITERATIONS = 3
    CONFIDENCE_THRESHOLD = 0.8
    MIN_CONVERGENCE_SCORE = 0.95


class IterativeReasoning:
    """迭代推理引擎"""
    
    def __init__(self):
        self.client = XinghuoClient()
        self.config = IterationConfig()
    
    def run_iterative_reasoning(
        self,
        user,
        query: str,
        initial_context: Dict,
        current_profile: Dict,
        max_iterations: int = None
    ) -> Dict:
        """
        运行迭代推理
        
        Args:
            user: 用户对象
            query: 当前查询
            initial_context: 初始上下文
            current_profile: 当前用户画像
            max_iterations: 最大迭代次数
            
        Returns:
            迭代推理结果
        """
        max_iterations = max_iterations or self.config.MAX_ITERATIONS
        
        iterations = []
        current_delta = {}
        best_result = None
        best_confidence = 0.0
        
        for iteration in range(1, max_iterations + 1):
            logger.info(f"Starting iteration {iteration}/{max_iterations}")
            
            # 构建迭代上下文
            iteration_context = self._build_iteration_context(
                user,
                query,
                initial_context,
                current_profile,
                current_delta,
                iteration
            )
            
            # 执行单次推理
            iteration_result = self._single_reasoning_iteration(iteration_context)
            
            # 评估收敛性
            is_converged, convergence_score = self._check_convergence(
                iteration_result,
                iterations
            )
            
            iteration_result['is_converged'] = is_converged
            iteration_result['convergence_score'] = convergence_score
            iterations.append(iteration_result)
            
            # 更新当前delta
            if iteration_result.get('delta'):
                current_delta = self._merge_delta(current_delta, iteration_result['delta'])
            
            # 跟踪最佳结果
            if iteration_result.get('confidence', 0) > best_confidence:
                best_confidence = iteration_result['confidence']
                best_result = iteration_result
            
            # 检查是否收敛
            if is_converged and iteration_result.get('confidence', 0) >= self.config.CONFIDENCE_THRESHOLD:
                logger.info(f"Iteration {iteration}: Converged with confidence {iteration_result['confidence']:.2f}")
                break
            
            logger.info(f"Iteration {iteration}: confidence={iteration_result.get('confidence', 0):.2f}, converged={is_converged}")
        
        # 返回最终结果
        return {
            'iterations': iterations,
            'final_delta': current_delta,
            'best_result': best_result,
            'converged': any(i.get('is_converged', False) for i in iterations),
            'total_iterations': len(iterations)
        }
    
    def _build_iteration_context(
        self,
        user,
        query: str,
        initial_context: Dict,
        current_profile: Dict,
        current_delta: Dict,
        iteration: int
    ) -> Dict:
        """构建迭代上下文"""
        
        context = {
            'user_id': user.id,
            'query': query,
            'iteration': iteration,
            'max_iterations': self.config.MAX_ITERATIONS
        }
        
        # 添加初始上下文
        context.update(initial_context)
        
        # 添加当前画像
        context['current_profile'] = self._summarize_profile_for_iteration(current_profile)
        
        # 添加之前的推理结果
        if current_delta:
            context['previous_deltas'] = current_delta
            context['reasoning_hint'] = f"这是第{iteration}次迭代，请基于之前的推理结果进行优化。"
        else:
            context['reasoning_hint'] = "这是第1次迭代，请进行初始推理。"
        
        return context
    
    def _single_reasoning_iteration(self, context: Dict) -> Dict:
        """执行单次推理迭代"""
        
        # 构建推理提示词
        prompt = self._build_reasoning_prompt(context)
        
        try:
            # 调用LLM
            response = self.client.generate_text(prompt)
            
            # 解析响应
            result = self._parse_reasoning_response(response)
            
            # 验证结果
            is_valid, issues = self._validate_iteration_result(result)
            
            result['is_valid'] = is_valid
            result['validation_issues'] = issues
            
            return result
        except Exception as e:
            logger.error(f"Iteration reasoning failed: {e}")
            return {
                'delta': {},
                'confidence': 0.0,
                'reasoning': f'推理失败: {str(e)}',
                'is_valid': False,
                'validation_issues': [str(e)]
            }
    
    def _build_reasoning_prompt(self, context: Dict) -> str:
        """构建推理提示词"""
        
        query = context.get('query', '')
        iteration = context.get('iteration', 1)
        current_profile = context.get('current_profile', {})
        previous_deltas = context.get('previous_deltas', {})
        history_context = context.get('history_context', '')
        multimodal_context = context.get('multimodal_context', '')
        reasoning_hint = context.get('reasoning_hint', '')
        
        # 格式化了历史上下文
        history_text = history_context if history_context else "暂无历史行为记录"
        
        # 格式化了多模态上下文
        multimodal_text = ""
        if multimodal_context:
            if isinstance(multimodal_context, dict):
                multimodal_text = json.dumps(multimodal_context, ensure_ascii=False, indent=2)
            else:
                multimodal_text = str(multimodal_context)
        
        prompt = f"""你是 USER-LLM R1 系统的核心推理引擎。

【任务】
基于用户输入和上下文，进行第 {iteration} 次迭代推理，推断用户画像的变化。

【当前用户输入】
{query}

【当前用户画像】
{json.dumps(current_profile, ensure_ascii=False, indent=2)}

【历史行为上下文】
{history_text}

【多模态分析结果】
{multimodal_text if multimodal_text else "无多模态输入"}

{reasoning_hint}
"""

        # 添加之前的迭代结果
        if previous_deltas:
            prompt += f"""
【之前的推理结果】
{json.dumps(previous_deltas, ensure_ascii=False, indent=2)}

请分析是否需要调整或补充之前的推理结果。
"""

        prompt += """
【推理要求】
1. 意图分析：识别用户的明确意图（提问/陈述/请求/困惑）
2. 历史对比：与历史行为进行对比，发现变化模式
3. 偏好推断：基于当前输入推断学习偏好变化
4. 合理性验证：检查推断结果的一致性和合理性
5. 置信度评估：给出本次推理的置信度

【输出格式】
请以JSON格式返回，包含以下字段：
- delta: 画像变化（dict），包含以下可选字段：
  - learning_preferences: 学习偏好变化
  - learning_goals: 学习目标变化
  - cognitive_style: 认知风格变化
  - knowledge_profile: 知识掌握度变化
  - misconceptions: 误解或易错点
- confidence: 置信度（0.0-1.0）
- reasoning: 推理过程说明（string）
- needs_refinement: 是否需要进一步迭代（boolean）
- refinement_hint: 如果需要进一步迭代，说明原因和建议

请确保输出合法的JSON，不要额外说明。

JSON:"""

        return prompt
    
    def _parse_reasoning_response(self, response: str) -> Dict:
        """解析推理响应"""
        try:
            result = json.loads(response)
            return self._normalize_iteration_result(result)
        except Exception:
            pass
        
        # 尝试提取JSON块
        import re
        match = re.search(r'\{[\s\S]*\}', response)
        if match:
            try:
                result = json.loads(match.group(0))
                return self._normalize_iteration_result(result)
            except Exception:
                pass
        
        logger.warning(f"Failed to parse reasoning response: {response[:200]}")
        return {
            'delta': {},
            'confidence': 0.0,
            'reasoning': '解析失败',
            'needs_refinement': True,
            'refinement_hint': '响应解析失败'
        }
    
    def _normalize_iteration_result(self, result: Dict) -> Dict:
        """规范化迭代结果"""
        normalized = {
            'delta': {},
            'confidence': result.get('confidence', 0.5),
            'reasoning': result.get('reasoning', ''),
            'needs_refinement': result.get('needs_refinement', False),
            'refinement_hint': result.get('refinement_hint', '')
        }
        
        # 提取delta
        delta = {}
        if 'delta' in result and isinstance(result['delta'], dict):
            for key in ['learning_preferences', 'learning_goals', 'cognitive_style', 
                       'knowledge_profile', 'misconceptions']:
                if key in result['delta']:
                    delta[key] = result['delta'][key]
        
        normalized['delta'] = delta
        
        # 确保confidence在有效范围
        try:
            normalized['confidence'] = max(0.0, min(1.0, float(normalized['confidence'])))
        except (ValueError, TypeError):
            normalized['confidence'] = 0.5
        
        return normalized
    
    def _validate_iteration_result(self, result: Dict) -> Tuple[bool, List[str]]:
        """验证迭代结果"""
        issues = []
        
        # 检查delta是否为空
        if not result.get('delta'):
            issues.append('delta为空，跳过本次推理')
        
        # 检查confidence是否合理
        confidence = result.get('confidence', 0.0)
        if confidence < 0.3:
            issues.append(f'置信度过低 ({confidence:.2f})')
        
        # 检查reasoning是否为空
        if not result.get('reasoning'):
            issues.append('推理过程说明为空')
        
        is_valid = len([i for i in issues if '为空' in i or '过低' in i]) == 0
        
        return is_valid, issues
    
    def _check_convergence(
        self,
        current_result: Dict,
        previous_iterations: List[Dict]
    ) -> Tuple[bool, float]:
        """检查收敛性"""
        if not previous_iterations:
            return False, 0.0
        
        # 比较与上次结果的相似度
        current_delta = current_result.get('delta', {})
        if not current_delta:
            return False, 0.0
        
        last_delta = previous_iterations[-1].get('delta', {})
        if not last_delta:
            return False, 0.0
        
        # 计算delta相似度
        similarity = self._calculate_delta_similarity(current_delta, last_delta)
        
        # 收敛判定：相似度超过阈值
        is_converged = similarity >= self.config.MIN_CONVERGENCE_SCORE
        
        return is_converged, similarity
    
    def _calculate_delta_similarity(self, delta1: Dict, delta2: Dict) -> float:
        """计算两次delta的相似度"""
        if not delta1 and not delta2:
            return 1.0
        
        if not delta1 or not delta2:
            return 0.0
        
        # 比较关键字段
        keys = set(delta1.keys()) | set(delta2.keys())
        if not keys:
            return 1.0
        
        match_count = 0
        for key in keys:
            val1 = str(delta1.get(key, ''))
            val2 = str(delta2.get(key, ''))
            if val1 == val2:
                match_count += 1
        
        return match_count / len(keys)
    
    def _merge_delta(self, base: Dict, new: Dict) -> Dict:
        """合并两次推理的delta"""
        if not base:
            return new.copy()
        if not new:
            return base.copy()
        
        merged = base.copy()
        
        for key, value in new.items():
            if key not in merged:
                merged[key] = value
            elif isinstance(value, dict) and isinstance(merged[key], dict):
                merged[key].update(value)
            elif isinstance(value, list) and isinstance(merged[key], list):
                # 合并列表（去重）
                merged[key] = list(set(merged[key] + value))
            else:
                # 新值覆盖旧值
                merged[key] = value
        
        return merged
    
    def _summarize_profile_for_iteration(self, profile: Dict) -> Dict:
        """为迭代推理总结画像"""
        summary = {
            'cognitive_style': profile.get('cognitive_style', 'unknown'),
            'learning_goals': profile.get('learning_goals', [])[:3],  # 最多3个
            'preferences': profile.get('learning_preferences', {}),
            'knowledge': list(profile.get('knowledge_profile', {}).keys())[:5]  # 最多5个
        }
        return summary


class ReasoningChainBuilder:
    """推理链构建器 - 构建完整的推理链"""
    
    @staticmethod
    def build_chain(
        query: str,
        context: Dict,
        iterations: List[Dict],
        final_delta: Dict,
        confidence: float
    ) -> Dict:
        """构建完整的推理链"""
        
        chain = {
            'query': query,
            'context_summary': ReasoningChainBuilder._summarize_context(context),
            'steps': [],
            'iterations': iterations,
            'final_delta': final_delta,
            'final_confidence': confidence,
            'created_at': datetime.now().isoformat()
        }
        
        # 添加每步的描述
        step_names = [
            '意图分析',
            '历史模式对比',
            '偏好变化推断',
            '合理性验证',
            '置信度评估'
        ]
        
        for i, name in enumerate(step_names):
            chain['steps'].append({
                'step': i + 1,
                'name': name,
                'status': 'completed',
                'description': f'推理链第{i+1}步: {name}'
            })
        
        return chain
    
    @staticmethod
    def _summarize_context(context: Dict) -> str:
        """总结上下文"""
        parts = []
        
        if context.get('query'):
            parts.append(f"查询: {context['query'][:50]}...")
        
        if context.get('history_events_count'):
            parts.append(f"历史事件: {context['history_events_count']}条")
        
        if context.get('multimodal_input'):
            parts.append("包含多模态输入")
        
        return '; '.join(parts) if parts else '无特殊上下文'
