import os
import sys
import logging
from base64 import b64decode

from PyQt6.QtWidgets import QApplication, QMainWindow, QTextEdit, QFrame
from PyQt6.QtCore import Qt, QRect, QMimeData, QEvent, QUrl
from PyQt6.QtGui import QFont, QFontMetrics, QFontDatabase, QImage, \
    QPainter, QColor, QKeyEvent, \
    QTextCursor, QTextLength, QTextCharFormat, QTextFrameFormat, QTextBlockFormat, \
    QTextDocument, QTextImageFormat, QTextTableCell, QTextTableFormat, QTextTableCellFormat

from qtconsole.pygments_highlighter import PygmentsHighlighter
from qtconsole.base_frontend_mixin import BaseFrontendMixin
from qtconsole.manager import QtKernelManager
from qtconsole.completion_widget import CompletionWidget

from IPython.core.inputtransformer2 import TransformerManager

from ansi2html import Ansi2HTMLConverter

light_theme = {
    'code_background': QColor('#ffffff'),
    'out_background': QColor('#fcfcfc'),
    'separater_color': QColor('#f8f8f8'),
    'done_color': QColor('#d4f4d4'),
    'pending_color': QColor('#fcfcfc'),
    'executing_color': QColor('#f5ca6e'),
    'error_color': QColor('#f4bdbd'),
    'inactive_color': QColor('#ffffff'),
    'active_color': QColor('#f4f4f4'),
}
theme = light_theme

class Highlighter(PygmentsHighlighter):
    def highlightBlock(self, string):
        # don't highlight output cells
        cursor = QTextCursor(self.currentBlock())
        table = cursor.currentTable()
        if table and table.cellAt(cursor).column() != 0:
                return
        return super().highlightBlock(string)

class CompletionWidget_(CompletionWidget):
    def _complete_current(self):
        super()._complete_current()
        self._text_edit.execute(self._text_edit.complete_cell_idx)

def get_table_cell_text(cell: QTextTableCell):
    text = ''
    cursor = cell.firstCursorPosition()
    while cursor.block() != cell.lastCursorPosition().block():
        text += cursor.block().text() + '\n'
        cursor.movePosition(QTextCursor.NextBlock)
    text += cursor.block().text()
    return text

def join_edit_block(fun):
    def wrapped(*args, **kwargs):
        self = args[0]
        self.edit_block_cursor.setPosition(self.textCursor().position())
        self.edit_block_cursor.joinPreviousEditBlock()
        ret = fun(*args, **kwargs)
        self.edit_block_cursor.endEditBlock()
        return ret
    return wrapped

class PyPadTextEdit(QTextEdit, BaseFrontendMixin):
    def __init__(self, parent):
        super().__init__(parent)

        # so ctrl+z won't undo initialization:
        self.setUndoRedoEnabled(False)

        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPixelSize(16)
        self.setFont(font)
        font_metrics = QFontMetrics(font)
        self.setTabStopDistance(4 * font_metrics.width(' '))

        Highlighter(self)

        self.setFrameStyle(QFrame.Shape.NoFrame)

        cursor = self.textCursor()
        table_format = QTextTableFormat()
        table_format.setBorder(0)
        table_format.setPadding(-3)
        table_format.setMargin(0)
        table_format.setWidth(QTextLength(QTextLength.PercentageLength, 100))
        table_format.setColumnWidthConstraints([
            QTextLength(QTextLength.PercentageLength, 50),
            QTextLength(QTextLength.PercentageLength, 50)])
        self.table = cursor.insertTable(1, 2, table_format)

        # if cell has execution result, this specifies the last execution count
        self.execution_count = [None]
        # if cell has an image
        self.has_image = [False]
        self.setTextCursor(self.code_cell(0).firstCursorPosition())

        self.document().begin().setVisible(False) # https://stackoverflow.com/questions/76061158

        self.cursorPositionChanged.connect(self.position_changed)

        os.environ['COLUMNS'] = '120'
        kernel_manager = QtKernelManager(kernel_name='python3')
        kernel_manager.start_kernel()

        kernel_client = kernel_manager.client()
        kernel_client.start_channels()

        self.kernel_manager = kernel_manager
        self.kernel_client = kernel_client

        kernel_client.kernel_info()

        self.execute_running = False
        self.execute_cell_idx = -1

        self.html_converter = Ansi2HTMLConverter()

        self._control = self # for CompletionWidget
        self.completion_widget = CompletionWidget_(self, 0)

        self.log = logging.getLogger('pypad')
        self.log.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        self.log.addHandler(handler)

        self.divider_drag = False
        self.setMouseTracking(True)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        self.transformer_manager = TransformerManager()

        self.edit_block_cursor = self.textCursor()

    def is_complete(self, code):
        return self.transformer_manager.check_complete(code)

    def code_cell(self, cell_idx):
        cell = self.table.cellAt(cell_idx, 0)
        assert cell.isValid()
        return cell

    def out_cell(self, cell_idx):
        cell = self.table.cellAt(cell_idx, 1)
        assert cell.isValid()
        return cell

    def insert_cell(self, cell_idx):
        '''insert cell before the given index'''
        self.table.insertRows(cell_idx, 1)
        self.execution_count.insert(cell_idx, None)
        self.has_image.insert(cell_idx, False)
        self.set_cell_pending(cell_idx)
        self.setTextCursor(self.code_cell(cell_idx).firstCursorPosition())

    def get_cell_code(self, cell_idx):
        return get_table_cell_text(self.code_cell(cell_idx))

    def position_changed(self):
        # fix selection to be within one column in table
        cursor = self.textCursor()
        if cursor.hasSelection():
            if cursor.position() < self.table.firstCursorPosition().position():
                cell = self.table.cellAt(cursor.anchor())
                if cell.isValid():
                    cursor.setPosition(self.table.cellAt(0, cell.column()).firstCursorPosition().position(), QTextCursor.KeepAnchor)
            if cursor.position() > self.table.lastCursorPosition().position():
                cell = self.table.cellAt(cursor.anchor())
                if cell.isValid():
                    cursor.setPosition(self.table.cellAt(self.table.rows()-1, cell.column()).lastCursorPosition().position(), QTextCursor.KeepAnchor)
        elif cursor.position() < self.table.firstCursorPosition().position():
            cursor.setPosition(self.table.firstCursorPosition().position())
        elif cursor.position() > self.table.lastCursorPosition().position():
            cursor.setPosition(self.code_cell(self.table.rows()-1).lastCursorPosition().position())
        self.setTextCursor(cursor)

        mrow, mrow_num, mcol, mcol_num = cursor.selectedTableCells()
        cell = self.table.cellAt(cursor)
        assert cell.isValid()
        if mcol_num > 1 or mrow_num > 1 or cell.column() == 1:
            cell_idx = -1
        else:
            cell_idx = cell.row()

        for i in range(self.table.rows()):
            self.set_cell_active(i, i == cell_idx)

    def pos_in_cell(self, cell_idx, cursor):
        cell = self.table.cellAt(cursor)
        if cell.isValid() and cell.row() == cell_idx:
            return cursor.position() - cell.firstCursorPosition().position()
        return -1

    @join_edit_block
    def set_cell_text(self, cell_idx, txt):
        cell = self.out_cell(cell_idx)
        cursor = cell.firstCursorPosition()
        cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)
        cursor.insertText(txt)
        self.has_image[self.execute_cell_idx] = False

    @join_edit_block
    def set_cell_img(self, cell_idx, img, format, name):
        # name should be unique to allow undo/redo
        cell = self.out_cell(cell_idx)
        cursor = cell.firstCursorPosition()
        cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)

        image = QImage()
        image.loadFromData(img, format.upper())
        self.document().addResource(QTextDocument.ImageResource, QUrl(name), image)
        image_format = QTextImageFormat()
        image_format.setName(name)
        image_format.setMaximumWidth(self.table.format().columnWidthConstraints()[1])
        cursor.insertImage(image_format)
        self.has_image[self.execute_cell_idx] = True

    @staticmethod
    def _out_cell_format(color):
        cell_format = QTextTableCellFormat()
        cell_format.setLeftBorder(3)
        cell_format.setLeftBorderStyle(QTextTableFormat.BorderStyle_Solid)
        cell_format.setLeftBorderBrush(color)
        cell_format.setLeftPadding(4)

        cell_format.setBottomBorder(1)
        cell_format.setBottomBorderStyle(QTextTableFormat.BorderStyle_Solid)
        cell_format.setBottomBorderBrush(theme['separater_color'])

        return cell_format

    @staticmethod
    def _code_cell_format(active):
        cell_format = QTextTableCellFormat()
        cell_format.setLeftBorder(3)
        cell_format.setLeftBorderStyle(QTextTableFormat.BorderStyle_Solid)
        cell_format.setLeftBorderBrush(theme['active_color'] if active else theme['inactive_color'])

        cell_format.setBottomBorder(1)
        cell_format.setBottomBorderStyle(QTextTableFormat.BorderStyle_Solid)
        cell_format.setBottomBorderBrush(theme['separater_color'])

        return cell_format

    # @join_edit_block
    def set_cell_active(self, cell_idx, active):
        return # todo: this is breaking the undo/redo stack
        self.code_cell(cell_idx).setFormat(self._code_cell_format(active))

    @join_edit_block
    def set_cell_done(self, cell_idx):
        self.out_cell(cell_idx).setFormat(self._out_cell_format(theme['done_color']))

    @join_edit_block
    def set_cell_pending(self, cell_idx):
        self.out_cell(cell_idx).setFormat(self._out_cell_format(theme['pending_color']))

    @join_edit_block
    def set_cell_executing(self, cell_idx):
        self.out_cell(cell_idx).setFormat(self._out_cell_format(theme['executing_color']))
        self.set_cell_text(cell_idx, '')

    @join_edit_block
    def set_cell_error(self, cell_idx, txt, tooltip=None):
        cell = self.out_cell(cell_idx)
        cell_format = self._out_cell_format(theme['error_color'])
        cell_format.setToolTip(tooltip)
        cell.setFormat(cell_format)
        self.set_cell_text(cell_idx, txt) # after setting tooltip

    def _execute(self, cell_idx, code=None):
        if code is None:
            code = self.get_cell_code(cell_idx)
        self.set_cell_executing(cell_idx)
        # force '_' to hold the previews cell output:
        if cell_idx > 0 and self.execution_count[cell_idx-1] is not None:
            code = '_ = _' + str(self.execution_count[cell_idx-1]) + '\n' + code
        # don't stop on error, we interrupt kernel and execute a new cell immediately after, otherwise might get aborted
        self.execute_msg_id = self.kernel_client.execute(code, False, stop_on_error=False)
        self.log.debug(f'execute [{cell_idx}] ({self.execute_msg_id.split('_')[-1]}): {code}')

    def execute(self, cell_idx, code=None):
        if self.execute_running:
            if self.execute_cell_idx < cell_idx:
                return # eventually we will execute this cell
            else:
                self.log.debug('interrupt kernel: new code')
                self.kernel_manager.interrupt_kernel()
        self.execute_running = True
        self.execute_cell_idx = cell_idx
        self._execute(cell_idx, code)
        for i in range(cell_idx+1, self.table.rows()):
            self.set_cell_pending(i)

    def _handle_execute_result(self, msg):
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'execute_result ({msg_id.split('_')[-1]})')
        if msg_id != self.execute_msg_id:
            return
        self._handle_execute_result_or_display_data(msg['content'], msg_id)

    def _handle_display_data(self, msg):
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'display_data ({msg_id.split('_')[-1]})')
        if msg_id != self.execute_msg_id:
            return
        self._handle_execute_result_or_display_data(msg['content'], msg_id)

    def _handle_execute_result_or_display_data(self, content, msg_id):
        data = content['data']
        if 'execution_count' in content: # only in execute_result
            self.execution_count[self.execute_cell_idx] = content['execution_count']
        if 'image/png' in data:
            image_data = b64decode(data['image/png'].encode('ascii'))
            self.set_cell_img(self.execute_cell_idx, image_data, 'PNG', msg_id)
        elif 'image/jpeg' in data:
            image_data = b64decode(data['image/jpeg'].encode('ascii'))
            self.set_cell_img(self.execute_cell_idx, image_data, 'JPG', msg_id)
        elif 'text/plain' in data:
            if not self.has_image[self.execute_cell_idx]:
                self.set_cell_text(self.execute_cell_idx, data['text/plain'])
        else:
            print(f'unsupported type {data}')

    def _handle_error(self, msg):
        msg_id = msg['parent_header']['msg_id']
        content = msg['content']
        ename = content['ename']
        self.log.debug(f'error ({msg_id.split('_')[-1]}): {ename}')
        if msg_id != self.execute_msg_id:
            return
        self.set_cell_error(self.execute_cell_idx, ename, self.html_converter.convert(''.join(content['traceback'])))

    def _handle_execute_reply(self, msg):
        msg_id = msg['parent_header']['msg_id']
        content = msg['content']
        status = content['status']
        self.log.debug(f'execute_reply ({msg_id.split('_')[-1]}): {status}')
        if msg_id != self.execute_msg_id:
            return
        if status == 'ok':
            self.set_cell_done(self.execute_cell_idx)
            if self.execute_cell_idx+1 < self.table.rows():
                self.execute_cell_idx = self.execute_cell_idx+1
                self._execute(self.execute_cell_idx)
            else:
                self.execute_running = False
        else:
            self.execution_count[self.execute_cell_idx] = None
            self.execute_running = False

    def _handle_complete_reply(self, msg):
        # code from qtconsole:
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'complete_reply ({msg_id.split('_')[-1]})')
        cursor = self.textCursor()
        if  (msg_id == self.complete_msg_id and
             self.pos_in_cell(self.complete_cell_idx, cursor) == self.complete_pos_in_cell and
             self.get_cell_code(self.complete_cell_idx) == self.complete_code):

            content = msg['content']
            matches = content['matches']
            start = content['cursor_start']
            end = content['cursor_end']

            start = max(start, 0)
            end = max(end, start)

            # Move the control's cursor to the desired end point
            cursor_pos_in_cell = self.complete_pos_in_cell
            if end < cursor_pos_in_cell:
                cursor.movePosition(QTextCursor.Left, n=(cursor_pos_in_cell - end))
            elif end > cursor_pos_in_cell:
                cursor.movePosition(QTextCursor.Right, n=(end - cursor_pos_in_cell))
            self.setTextCursor(cursor)
            offset = end - start
            # Move the local cursor object to the start of the match and complete
            cursor.movePosition(QTextCursor.Left, n=offset)
            self.completion_widget.cancel_completion()

            if len(matches) == 1:
                cursor.setPosition(self.textCursor().position(), QTextCursor.KeepAnchor)
                cursor.insertText(matches[0])

            elif len(matches) > 1:
                current_pos = self.textCursor().position()
                prefix = os.path.commonprefix(matches)
                if prefix:
                    cursor.setPosition(current_pos, QTextCursor.KeepAnchor)
                    cursor.insertText(prefix)
                    current_pos = cursor.position()
                self.completion_widget.show_items(cursor, matches, prefix_length=len(prefix))

    def _handle_kernel_info_reply(self, msg):
        self.log.debug(f'kernel_info_reply')
        language_info = msg['content']['language_info']
        self.kernel_info = language_info['name'] + language_info['version']
        self.setUndoRedoEnabled(True)
        self.parent().show()

    def _handle_clear_output(self, msg):
        self.log.debug(f'clear_output')

    def _handle_exec_callback(self, msg):
        self.log.debug(f'exec_callback')

    def _handle_input_request(self, msg):
        self.log.debug(f'input_request')

    def _handle_inspect_reply(self, rep):
        self.log.debug(f'inspect_reply')

    def _handle_shutdown_reply(self, msg):
        self.log.debug(f'shutdown_reply')

    def _handle_status(self, msg):
        return

    def _handle_stream(self, msg):
        print(msg['content']['text'], end='')

    def _handle_kernel_restarted(self, died=True):
        self.log.debug(f'kernel_restarted')

    def _handle_kernel_died(self, since_last_heartbeat):
        self.log.debug(f'kernel_died {since_last_heartbeat}')

    def keyPressEvent(self, e):
        # operations that always propegate:
        if e.key() in [Qt.Key_Z, Qt.Key_Y] and (e.modifiers() & Qt.ControlModifier):
            return super().keyPressEvent(e)
        elif e.key() == Qt.Key_V and (e.modifiers() & Qt.ControlModifier):
            return super().keyPressEvent(e) # paste handled by insertFromMimeData

        self.edit_block_cursor.setPosition(self.textCursor().position())
        self.edit_block_cursor.beginEditBlock()
        self.keyPressEvent2(e)
        self.edit_block_cursor.endEditBlock()

    def keyPressEvent2(self, e):
        cursor = self.textCursor()
        if e.key() == Qt.Key_C and (e.modifiers() & Qt.ControlModifier):
            if cursor.hasSelection():
                QApplication.instance().clipboard().setText(cursor.selection().toPlainText())
            else:
                self.log.debug('interrupt kernel: ctrl+c')
                self.kernel_manager.interrupt_kernel()
            return
        elif e.key() == Qt.Key_A and (e.modifiers() & Qt.ControlModifier):
            cursor = self.code_cell(0).firstCursorPosition()
            cursor.setPosition(self.code_cell(self.table.rows()-1).lastCursorPosition().position(), QTextCursor.KeepAnchor)
            self.setTextCursor(cursor)
            return
        if cursor.currentTable() != self.table:
            return
        mrow, mrow_num, mcol, mcol_num = cursor.selectedTableCells()
        cell = self.table.cellAt(cursor)
        assert cell.isValid()
        col = cell.column()
        cell_idx = cell.row()
        # allow navigation keys to propegate, restrict navigation to one column
        if e.key() in [Qt.Key_Up, Qt.Key_Down]:
            return super().keyPressEvent(e)
        elif e.key() == Qt.Key_Left:
            if cell.firstCursorPosition().position() == cursor.position() or mrow_num > 1:
                if cell_idx > 0:
                    cursor.setPosition(self.table.cellAt(cell_idx-1, col).lastCursorPosition().position(),
                                       QTextCursor.KeepAnchor if (e.modifiers() & Qt.ShiftModifier) else QTextCursor.MoveAnchor)
                    self.setTextCursor(cursor)
                return
            return super().keyPressEvent(e)
        elif e.key() == Qt.Key_Right:
            if cell.lastCursorPosition().position() == cursor.position() or mrow_num > 1:
                if cell_idx + 1 < self.table.rows():
                    cursor.setPosition(self.table.cellAt(cell_idx+1, col).firstCursorPosition().position(),
                                       QTextCursor.KeepAnchor if (e.modifiers() & Qt.ShiftModifier) else QTextCursor.MoveAnchor)
                    self.setTextCursor(cursor)
                return
            return super().keyPressEvent(e)
        if col == 1 or mcol_num > 1:
            return

        if e.key() == Qt.Key_Return:
            if e.modifiers() & Qt.ShiftModifier:
                pass # shift+enter always adds a new line
            else:
                cursor.setPosition(self.code_cell(cell_idx).firstCursorPosition().position(), QTextCursor.KeepAnchor)
                is_complete, indent = self.is_complete(cursor.selection().toPlainText())
                if is_complete == 'incomplete':
                    cursor.setPosition(cursor.anchor())
                    self.textCursor().insertText('\n' + indent*' ')
                    self.execute(cell_idx)
                else: # 'complete' or 'invalid', add a new cell below
                    self.insert_cell(cell_idx+1)
                    cursor.setPosition(self.code_cell(cell_idx).lastCursorPosition().position(), QTextCursor.KeepAnchor)
                    code = cursor.selection().toPlainText()
                    cursor.removeSelectedText()
                    cursor = self.code_cell(cell_idx+1).firstCursorPosition()
                    cursor.insertText(code)
                    self.setTextCursor(self.code_cell(cell_idx+1).firstCursorPosition())
                    if code == '': # no need to re-execute current
                        self.execute(cell_idx + 1)
                    else:
                        self.execute(cell_idx)
                return
        elif e.key() == Qt.Key_Backspace:
            if mrow_num > 1:
                if mrow == 0 and mrow_num == self.table.rows():
                    self.insert_cell(0)
                    mrow += 1
                self.table.removeRows(mrow, mrow_num)
                return
            if cursor.position() == self.code_cell(cell_idx).firstCursorPosition().position():
                if cell_idx > 0:
                    code = self.get_cell_code(cell_idx)
                    self.table.removeRows(cell_idx, 1)
                    cursor = self.code_cell(cell_idx-1).lastCursorPosition()
                    pos = cursor.position()
                    cursor.insertText(code)
                    cursor.setPosition(pos)
                    self.setTextCursor(cursor)
                    self.execute(cell_idx-1)
                return
        elif e.key() == Qt.Key_Delete:
            if mrow_num > 1:
                if mrow == 0 and mrow_num == self.table.rows():
                    self.insert_cell(0)
                    mrow += 1
                self.table.removeRows(mrow, mrow_num)
                return
            if (not cursor.hasSelection() and
                cursor.position() == self.code_cell(cell_idx).lastCursorPosition().position() and
                cell_idx+1 < self.table.rows()):
               pos = cursor.position()
               cursor.insertText(self.get_cell_code(cell_idx+1))
               cursor.setPosition(pos)
               self.setTextCursor(cursor)
               self.table.removeRows(cell_idx+1, 1)
               self.execute(cell_idx)
               return
        elif e.key() == Qt.Key_Tab:
            if not cursor.hasSelection():
                check_cursor = QTextCursor(cursor)
                check_cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor)
                if check_cursor.hasSelection() and not check_cursor.selectedText().isspace():
                    self.complete_cell_idx = cell_idx
                    self.complete_pos_in_cell = self.pos_in_cell(cell_idx, cursor)
                    self.complete_code = self.get_cell_code(cell_idx)
                    self.complete_msg_id = self.kernel_client.complete(code=self.complete_code, cursor_pos=self.complete_pos_in_cell)
                    return

        old_code = self.get_cell_code(cell_idx)
        super().keyPressEvent(e)
        code = self.get_cell_code(cell_idx)
        if code != old_code:
            self.execute(cell_idx, code)

    def insertFromMimeData(self, source: QMimeData):
        lines = source.text().splitlines()
        lines.reverse()
        cursor = self.textCursor()
        cursor.insertText(lines.pop())
        cell = self.table.cellAt(cursor)
        if cell.isValid():
            self.execute(cell.row())
        while lines:
            self.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key_Return,
                    Qt.KeyboardModifier.NoModifier, '\r', False, 0))
            self.textCursor().insertText(lines.pop())

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        rect = self.viewport().rect()

        table_format = self.table.format()
        divider_precentage = table_format.columnWidthConstraints()[0].rawValue()

        editor_rect = QRect(rect.x(), rect.y(), int(rect.width() * divider_precentage/100), rect.height())
        painter.fillRect(editor_rect, theme['code_background'])
        out_rect = QRect(rect.x()+editor_rect.width(), rect.y(), rect.width() - editor_rect.width(), rect.height())
        painter.fillRect(out_rect, theme['out_background'])

        super().paintEvent(event)

    def move_divider(self, delta_x):
        table_format = self.table.format()
        divider_precentage = table_format.columnWidthConstraints()[0].rawValue()
        divider_precentage += 100*delta_x/self.viewport().width()
        table_format.setColumnWidthConstraints([
            QTextLength(QTextLength.PercentageLength, divider_precentage),
            QTextLength(QTextLength.PercentageLength, 100 - divider_precentage)])
        self.table.setFormat(table_format)

    def get_divider_x(self):
        table_format = self.table.format()
        return self.viewport().width() * table_format.columnWidthConstraints()[0].rawValue()/100

    def near_divider(self, x):
        margin = 5
        return abs(x - self.get_divider_x()) < margin

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.near_divider(event.pos().x()):
            self.divider_drag_start_pos = event.pos()
            self.divider_drag = True
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.divider_drag:
            delta_x = event.pos().x() - self.divider_drag_start_pos.x()
            self.move_divider(delta_x)
            self.divider_drag_start_pos = event.pos()
        else:
            if self.near_divider(event.pos().x()):
                self.viewport().setCursor(Qt.SplitHCursor)
            else:
                self.viewport().unsetCursor()
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.divider_drag = False
        else:
            super().mouseReleaseEvent(event)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle('pypad')
        self.setGeometry(100, 100, 800, 600)

        self.pypad_text_edit = PyPadTextEdit(self)
        self.setCentralWidget(self.pypad_text_edit)

def main():
    app = QApplication(sys.argv)
    main_window = MainWindow()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()

