"""课程生成历史记录

追踪和管理课程生成的完整过程，包括版本历史和优化记录。
"""
import json
from datetime import datetime
from typing import Dict, List, Optional


class GenerationHistory:
    """生成历史记录管理器"""
    
    def __init__(self, user, topic: str):
        self.user = user
        self.topic = topic
        self.records: List[Dict] = []
        self.current_version = 0
    
    def add_record(self, record_type: str, data: Dict, version: Optional[int] = None) -> int:
        """添加生成记录
        
        Args:
            record_type: 记录类型（draft/review/revision/final）
            data: 记录数据
            version: 版本号（可选，自动递增）
        
        Returns:
            记录ID
        """
        if version is None:
            self.current_version += 1
            version = self.current_version
        
        record = {
            'id': len(self.records) + 1,
            'version': version,
            'type': record_type,
            'timestamp': datetime.now().isoformat(),
            'data': data,
        }
        
        self.records.append(record)
        return record['id']
    
    def add_draft(self, content: str, resource_type: str, metadata: Dict = None) -> int:
        """添加初稿记录"""
        return self.add_record('draft', {
            'content': content,
            'resource_type': resource_type,
            'metadata': metadata or {},
        })
    
    def add_review(self, review_data: Dict) -> int:
        """添加审核记录"""
        return self.add_record('review', review_data)
    
    def add_revision(self, original_version: int, revised_content: str, 
                    revision_reason: str) -> int:
        """添加修订记录"""
        return self.add_record('revision', {
            'original_version': original_version,
            'revised_content': revised_content,
            'revision_reason': revision_reason,
        })
    
    def add_final(self, content: str, quality_score: float, 
                 iterations: int) -> int:
        """添加最终版本记录"""
        return self.add_record('final', {
            'content': content,
            'quality_score': quality_score,
            'iterations': iterations,
        })
    
    def get_latest_version(self) -> Optional[Dict]:
        """获取最新版本"""
        if not self.records:
            return None
        return self.records[-1]
    
    def get_version_history(self, resource_type: str = None) -> List[Dict]:
        """获取版本历史"""
        if resource_type:
            return [
                r for r in self.records 
                if r['type'] in ['draft', 'revision', 'final'] 
                and r['data'].get('resource_type') == resource_type
            ]
        return [r for r in self.records if r['type'] in ['draft', 'revision', 'final']]
    
    def compare_versions(self, version1: int, version2: int) -> Dict:
        """比较两个版本的差异"""
        v1 = next((r for r in self.records if r['version'] == version1), None)
        v2 = next((r for r in self.records if r['version'] == version2), None)
        
        if not v1 or not v2:
            return {'error': '版本不存在'}
        
        return {
            'version1': v1,
            'version2': v2,
            'timestamp_diff': v2['timestamp'] + ' - ' + v1['timestamp'],
        }
    
    def get_optimization_summary(self) -> Dict:
        """获取优化总结"""
        drafts = [r for r in self.records if r['type'] == 'draft']
        reviews = [r for r in self.records if r['type'] == 'review']
        revisions = [r for r in self.records if r['type'] == 'revision']
        finals = [r for r in self.records if r['type'] == 'final']
        
        summary = {
            'total_records': len(self.records),
            'draft_count': len(drafts),
            'review_count': len(reviews),
            'revision_count': len(revisions),
            'iterations': len(drafts),
        }
        
        if finals:
            final = finals[-1]
            summary['final_quality_score'] = final['data'].get('quality_score', 0)
            summary['final_iterations'] = final['data'].get('iterations', 0)
        
        return summary
    
    def export_history(self) -> str:
        """导出历史记录为JSON"""
        return json.dumps({
            'topic': self.topic,
            'user': str(self.user.id) if self.user else None,
            'records': self.records,
        }, ensure_ascii=False, indent=2)
    
    def import_history(self, history_json: str) -> bool:
        """导入历史记录"""
        try:
            data = json.loads(history_json)
            self.records = data.get('records', [])
            self.current_version = max(
                (r['version'] for r in self.records), 
                default=0
            )
            return True
        except Exception:
            return False


class GenerationSession:
    """生成会话管理器"""
    
    def __init__(self, user, topic: str, grade_level: str = 'college'):
        self.user = user
        self.topic = topic
        self.grade_level = grade_level
        self.created_at = datetime.now()
        self.updated_at = self.created_at
        self.status = 'initialized'  # initialized/planning/generating/reviewing/completed/failed
        self.current_phase = 'planning'
        self.history = GenerationHistory(user, topic)
        self.metadata: Dict = {}
    
    def start_generation(self):
        """开始生成"""
        self.status = 'generating'
        self.updated_at = datetime.now()
    
    def set_phase(self, phase: str):
        """设置当前阶段"""
        self.current_phase = phase
        self.updated_at = datetime.now()
    
    def complete(self, final_content: str, quality_score: float):
        """完成生成"""
        self.status = 'completed'
        self.updated_at = datetime.now()
        self.history.add_final(final_content, quality_score, 
                              len(self.history.get_version_history()))
    
    def fail(self, error: str):
        """生成失败"""
        self.status = 'failed'
        self.updated_at = datetime.now()
        self.metadata['error'] = error
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'topic': self.topic,
            'grade_level': self.grade_level,
            'status': self.status,
            'current_phase': self.current_phase,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'optimization_summary': self.history.get_optimization_summary(),
        }


class GenerationTracker:
    """全局生成追踪器"""
    
    _sessions: Dict[str, GenerationSession] = {}
    
    @classmethod
    def create_session(cls, user, topic: str, grade_level: str = 'college') -> GenerationSession:
        """创建新的生成会话"""
        session_id = f"{user.id}_{topic}_{datetime.now().timestamp()}"
        session = GenerationSession(user, topic, grade_level)
        cls._sessions[session_id] = session
        return session
    
    @classmethod
    def get_session(cls, session_id: str) -> Optional[GenerationSession]:
        """获取会话"""
        return cls._sessions.get(session_id)
    
    @classmethod
    def get_user_sessions(cls, user) -> List[GenerationSession]:
        """获取用户的所有会话"""
        return [s for s in cls._sessions.values() if s.user.id == user.id]
    
    @classmethod
    def get_topic_sessions(cls, topic: str) -> List[GenerationSession]:
        """获取主题的所有会话"""
        return [s for s in cls._sessions.values() if s.topic == topic]
    
    @classmethod
    def cleanup_old_sessions(cls, hours: int = 24):
        """清理旧会话"""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=hours)
        cls._sessions = {
            k: v for k, v in cls._sessions.items() 
            if v.updated_at > cutoff
        }
