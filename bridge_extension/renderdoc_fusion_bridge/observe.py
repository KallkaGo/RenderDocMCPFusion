"""Observe-layer composition for the live bridge."""

from .domains.base import BridgeService
from .domains.buffer import BufferServiceMixin
from .domains.capture import CaptureStatusService
from .domains.export import ExportServiceMixin
from .domains.mesh import MeshServiceMixin
from .domains.postvs import PostVSExportServiceMixin
from .domains.inventory import CaptureInventoryServiceMixin
from .domains.packets import PacketServiceMixin
from .domains.pipeline import PipelineStateServiceMixin
from .domains.resource_support import ResourceSupportMixin
from .domains.search import EventSearchMixin
from .domains.shader import ShaderServiceMixin
from .domains.shader_debug import ShaderDebugServiceMixin
from .domains.texture import TextureServiceMixin
from .domains.analysis import AnalysisServiceMixin
from .domains.bundle_export import BundleExportServiceMixin
from .domains.query_api import QueryAPIServiceMixin


class ObserveService(
    AnalysisServiceMixin,
    BundleExportServiceMixin,
    QueryAPIServiceMixin,
    PostVSExportServiceMixin,
    ShaderDebugServiceMixin,
    CaptureInventoryServiceMixin,
    EventSearchMixin,
    PacketServiceMixin,
    PipelineStateServiceMixin,
    BufferServiceMixin,
    ShaderServiceMixin,
    TextureServiceMixin,
    ExportServiceMixin,
    MeshServiceMixin,
    ResourceSupportMixin,
    BridgeService,
):
    """Compose observe-only live bridge services from domain mixins."""
