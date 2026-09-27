import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import santa

class WorkflowTests(unittest.TestCase):
    def test_pool_winter_is_converted_to_sun(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            santa.write_plan(run, {'images': [{'file':'01.jpg','action':'skip'}, {'file':'02.jpg','action':'TODO'}]})
            (run/'classify.json').write_text(json.dumps({'02.jpg': {'action':'transform','role':'lifestyle','mode':'winter','summer':True,'seen':'Adults in a pool wearing swimwear','scene':'Lights along the fence, snow on the ground.'}}))
            santa.apply_classify(run)
            img = santa.read_plan(run)['images'][1]
            self.assertEqual(img['mode'], 'sun')
            self.assertFalse(img['summer'])
            self.assertNotIn('snow', img['scene'])
            self.assertIn('No snow', santa.build_prompt(img))

    def test_prepare_preserves_pending_job_timestamp(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run/'originals').mkdir()
            Image.new('RGB',(32,32),'blue').save(run/'originals/02.jpg')
            santa.write_plan(run, {'images':[{'file':'02.jpg','action':'transform','role':'lifestyle','scene':'Tree behind sofa','mode':'scene'}]})
            first = santa.prepare_jobs(run)[0]
            second = santa.prepare_jobs(run)[0]
            self.assertEqual(first['prepared_at'],second['prepared_at'])

    def test_no_hat_scene_does_not_emit_hat_suggestion(self):
        prompt = santa.build_prompt({'mode':'scene','scene':'Tree behind sofa. No Santa hats.'})
        self.assertNotIn('At most one natural red Santa hat',prompt)

    def test_uncertain_framing_requires_visual_check(self):
        from unittest.mock import patch
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);(run/'originals').mkdir();(run/'christmas').mkdir()
            Image.new('RGB',(32,32),'white').save(run/'originals/02.jpg')
            Image.new('RGB',(32,32),'white').save(run/'christmas/02.png')
            santa.write_plan(run, {'images':[{'file':'02.jpg','action':'transform'}]})
            (run/'verify.json').write_text(json.dumps({'02.jpg':{k:True for k in santa.VERIFY_KEYS}}))
            with patch.object(santa,'framing_check',return_value={'scale':1.0,'shift':0.0,'score':0.1}):
                santa.apply_verify(run)
            self.assertFalse(santa.read_plan(run)['images'][0]['stage_ready'])

if __name__ == '__main__': unittest.main()
