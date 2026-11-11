"""Transport2 campaign entry, retaining frozen identity-fixed common runner."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src/gqltest').is_dir():raise RuntimeError('Deploy entry under project scripts')
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'src')]
import campaign_evolving_graph as campaign
from gecko_transport2_codec_20261109 import install
install(campaign)
if __name__=='__main__':raise SystemExit(campaign.main())
