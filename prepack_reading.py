"""Request-local semantic reading before final evidence packing.

The caller supplies the already authorized native pool. This stage can suggest
exact quotes to the existing selector, never grant access or certify an answer.
"""
from copy import deepcopy
from dataclasses import dataclass

from candidate_admission import order_by_query_frontiers
from source_reader import read_sources, selection_preferences
from user_task import UserTask

VERSION='native-frontier-reading-v1'


@dataclass(frozen=True)
class CandidateReading:
    """Eligible candidate pool, exact passage preferences and diagnostics before final context packing."""
    pool: list[dict]
    preferences: tuple[dict,...]
    diagnostics: dict


def read_candidate_frontiers(user_task,items,*,rank_context,max_chars,complete):
    """Read a bounded candidate view and return validated preferences; final packing must still prove quote retention."""
    if not isinstance(user_task,UserTask):
        raise TypeError('User-origin task required')
    # IDs are an explicit temporary address for every native candidate. They
    # do not change its native identity, revision, facts, eligibility or rank.
    pool=[{**deepcopy(item),'evidence_id':f'E{n+1}'} for n,item in enumerate(items)]
    diagnostics={'version':VERSION,'enabled':True,'assessed':False,'applied':False,
                 'pool_count':len(pool),'reader_count':0,'preferred_passages':0}
    if max_chars<=0 or not pool:
        return CandidateReading(pool,(),{**diagnostics,'reason':'empty-or-unfit-pool'})
    ordered=order_by_query_frontiers(pool,max_chars=2*max_chars,query_rank_context=rank_context)
    offered=[];used=0
    for item in ordered:
        text=item.get('text')
        if (item.get('status')=='deleted' or not isinstance(text,str) or not text.strip()
                or used+len(text)>2*max_chars):
            continue
        offered.append(item);used+=len(text)
        if len(offered)==10:break
    diagnostics.update(reader_count=len(offered),reader_chars=used)
    if not offered:
        return CandidateReading(pool,(),{**diagnostics,'reason':'no-whole-fitting-record'})
    plan={'canonical_question':user_task.resolved_user_context,'requirements':[user_task.question],
          'exclusions':[]}
    reading=read_sources(user_task.question,plan,offered,complete,user_task=user_task)
    preferences=selection_preferences(reading,user_task.question,plan,offered,user_task=user_task)
    diagnostics.update(assessed=reading.assessed,preferred_passages=len(preferences),
                       warnings=list(reading.warnings),
                       reason='validated-quote-preferences' if preferences else 'no-validated-preferences')
    # Final packing must validate quote retention. Assessed is deliberately not
    # named applied or complete: the packer may be unable to retain a quote.
    return CandidateReading(pool,tuple(preferences),diagnostics)
