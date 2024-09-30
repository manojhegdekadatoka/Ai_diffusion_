import json

from enum import Enum
from dataclasses import dataclass
from typing import Any, NamedTuple
from pathlib import Path
from PyQt5.QtCore import Qt, QObject, QAbstractListModel, QSortFilterProxyModel, QModelIndex
from PyQt5.QtCore import pyqtSignal

from .comfy_workflow import ComfyWorkflow
from .connection import Connection
from .properties import Property, ObservableProperties
from .util import user_data_dir, client_logger as log


class WorkflowSource(Enum):
    document = 0
    remote = 1
    local = 2


@dataclass
class CustomWorkflow:
    id: str
    source: WorkflowSource
    graph: dict
    path: Path | None = None

    @property
    def name(self):
        return self.id.removesuffix(".json")

    @property
    def workflow(self):
        return ComfyWorkflow.import_graph(self.graph)


class WorkflowCollection(QAbstractListModel):
    def __init__(self, connection: Connection, folder: Path | None = None):
        super().__init__()
        self._workflows: list[CustomWorkflow] = []

        self._folder = folder or user_data_dir / "workflows"
        for file in self._folder.glob("*.json"):
            try:
                self._process_file(file)
            except Exception as e:
                log.exception(f"Error loading workflow from {file}: {e}")

        self._connection = connection
        self._connection.workflow_published.connect(self._process_remote_workflow)
        for wf in self._connection.workflows.keys():
            self._process_remote_workflow(wf)

    def _process_remote_workflow(self, id: str):
        self._process(CustomWorkflow(id, WorkflowSource.remote, self._connection.workflows[id]))

    def _process_file(self, file: Path):
        with file.open("r") as f:
            self._process(CustomWorkflow(file.stem, WorkflowSource.local, json.load(f)))

    def _process(self, workflow: CustomWorkflow):
        idx = self.find_index(workflow.id)
        if idx.isValid():
            self.set_graph(idx, workflow.graph)
        else:
            self.append(workflow)

    def rowCount(self, parent=QModelIndex()):
        return len(self._workflows)

    def data(self, index: QModelIndex, role: int = 0):
        if role == Qt.ItemDataRole.DisplayRole:
            return self._workflows[index.row()].name
        if role == Qt.ItemDataRole.UserRole:
            return self._workflows[index.row()].id

    def append(self, item: CustomWorkflow):
        end = len(self._workflows)
        self.beginInsertRows(QModelIndex(), end, end)
        self._workflows.append(item)
        self.endInsertRows()

    def set_graph(self, index: QModelIndex, graph: dict):
        self._workflows[index.row()].graph = graph
        self.dataChanged.emit(index, index)

    def find_index(self, id: str):
        for i, wf in enumerate(self._workflows):
            if wf.id == id:
                return self.index(i)
        return QModelIndex()

    def find(self, id: str):
        idx = self.find_index(id)
        if idx.isValid():
            return self._workflows[idx.row()]
        return None

    def get(self, id: str):
        result = self.find(id)
        if result is None:
            raise KeyError(f"Workflow {id} not found")
        return result

    def __getitem__(self, index: int):
        return self._workflows[index]

    def __len__(self):
        return len(self._workflows)


class SortedWorkflows(QSortFilterProxyModel):
    def __init__(self, workflows: WorkflowCollection):
        super().__init__()
        self._workflows = workflows
        self.setSourceModel(workflows)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.sort(0)

    def lessThan(self, left: QModelIndex, right: QModelIndex):
        l = self._workflows[left.row()]
        r = self._workflows[right.row()]
        if l.source is r.source:
            return l.name < r.name
        return l.source.value < r.source.value

    def __getitem__(self, index: int):
        idx = self.mapToSource(self.index(index, 0)).row()
        return self._workflows[idx]


class ParamKind(Enum):
    image_layer = 0
    mask_layer = 1
    number_int = 2


class CustomParam(NamedTuple):
    kind: ParamKind
    name: str
    default: Any | None = None
    min: int | None = None
    max: int | None = None


def _gather_params(w: ComfyWorkflow):
    for node in w:
        match node.type:
            case "ETN_KritaImageLayer":
                name = node.input("name", "Image")
                yield CustomParam(ParamKind.image_layer, name)
            case "ETN_KritaMaskLayer":
                name = node.input("name", "Mask")
                yield CustomParam(ParamKind.mask_layer, name)
            case "ETN_IntParameter":
                name = node.input("name", "Parameter")
                default = node.input("default", 0)
                min = node.input("min", -(2**31))
                max = node.input("max", 2**31)
                yield CustomParam(ParamKind.number_int, name, default=default, min=min, max=max)


class CustomWorkspace(QObject, ObservableProperties):

    workflow_id = Property("", setter="_set_workflow_id")
    params = Property({}, persist=True)

    workflow_id_changed = pyqtSignal(str)
    graph_changed = pyqtSignal()
    params_changed = pyqtSignal(dict)
    modified = pyqtSignal(QObject, str)

    def __init__(self, workflows: WorkflowCollection):
        super().__init__()
        self._workflows = workflows
        self._workflow: CustomWorkflow | None = None
        self._graph: ComfyWorkflow | None = None
        self._metadata: list[CustomParam] = []

        workflows.dataChanged.connect(self._update_workflow)
        workflows.rowsInserted.connect(self._set_default_workflow)
        self._set_default_workflow()

    def _set_default_workflow(self):
        if not self.workflow_id and len(self._workflows) > 0:
            self.workflow_id = self._workflows[0].id

    def _update_workflow(self, idx: QModelIndex, _: QModelIndex):
        wf = self._workflows[idx.row()]
        if wf.id == self._workflow_id:
            self._workflow = wf
            self._graph = self._workflow.workflow
            self._metadata = list(_gather_params(self._graph))
            self.params = _coerce(self.params, self._metadata)
            self.graph_changed.emit()

    def _set_workflow_id(self, id: str):
        if self._workflow_id == id:
            return
        self._workflow_id = id
        self.workflow_id_changed.emit(id)
        self.modified.emit(self, "workflow_id")
        self._update_workflow(self._workflows.find_index(id), QModelIndex())

    def set_graph(self, id: str, graph: dict):
        if self._workflows.find(id) is None:
            self._workflows.append(CustomWorkflow(id, WorkflowSource.document, graph))
        self.workflow_id = id

    @property
    def workflow(self):
        return self._workflow

    @property
    def graph(self):
        return self._graph

    @property
    def metadata(self):
        return self._metadata


def _coerce(params: dict[str, Any], types: list[CustomParam]):
    def use(value, default):
        if value is None or not type(value) == type(default):
            return default
        return value

    return {t.name: use(params.get(t.name), t.default) for t in types}