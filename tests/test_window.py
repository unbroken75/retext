# vim: ts=4:sw=4:expandtab

# This file is part of ReText
# Copyright: 2016 Maurice van der Pot
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import platform
import sys
import tempfile
import unittest
import warnings
from contextlib import suppress
from unittest.mock import MagicMock, patch

import markups
from markups.abstract import ConvertedMarkup
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

import ReText
from ReText.tab import PreviewDisabled, PreviewLive, PreviewNormal
from ReText.window import MAX_REWATCH_ATTEMPTS, REWATCH_RETRY_INTERVAL, ReTextWindow

path_to_testdata = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'testdata')

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
app = QApplication.instance() or QApplication(sys.argv)

def handle_timer_event():
    print('timer event received')


class FakeConverterProcess(QObject):
    conversionDone = pyqtSignal()

    def start_conversion(self, name, filename, extensions, text, current_dir):
        self.conversionDone.emit()

    def get_result(self):
        return ConvertedMarkup('')

@patch('ReText.tab.converterprocess.ConverterProcess', FakeConverterProcess)
class TestWindow(unittest.TestCase):

    def setUp(self):
        warnings.simplefilter("ignore", Warning)
        self.readListFromSettingsMock = patch('ReText.readListFromSettings', return_value=[]).start()
        self.writeListToSettingsMock = patch('ReText.writeListToSettings').start()
        self.globalSettingsMock = patch(
            'ReText.window.globalSettings',
            MagicMock(**ReText.configOptions),
        ).start()
        # configOptions only holds the option values, so the mock has no
        # working getPreviewFont: without this, any tab left in a preview
        # state passes a MagicMock to QTextDocument.setDefaultFont, and
        # the exception in the slot takes the whole process down.
        self.globalSettingsMock.getPreviewFont.return_value = QFont()
        self.globalCacheMock = patch(
            'ReText.window.globalCache',
            MagicMock(**ReText.cacheOptions),
        ).start()
        self.fileSystemWatcherPatcher = patch('ReText.window.QFileSystemWatcher')
        self.fileSystemWatcherMock = self.fileSystemWatcherPatcher.start()
        ReText.tab.globalSettings = self.globalSettingsMock

    def tearDown(self):
        patch.stopall()


    #
    # Helper functions
    #

    @staticmethod
    def get_ui_enabled_states(window):
        enabled = set()
        disabled = set()

        for item in ('actionBold',
                     'actionCopy',
                     'actionCut',
                     'actionItalic',
                     'actionUnderline',
                     'actionUndo',
                     'actionRedo',
                     'actionReload',
                     'actionSave',
                     'actionSetEncoding',
                     'editBar',
                     'formattingBox',
                     'symbolBox'):
            if getattr(window, item).isEnabled():
                enabled.add(item)
            else:
                disabled.add(item)

        return enabled, disabled

    def check_widget_state(self, window, expected_enabled, expected_disabled):
        actually_enabled, actually_disabled = self.get_ui_enabled_states(window)

        self.assertEqual(
            expected_enabled - actually_enabled,
            set(),
            'These widgets are unexpectedly disabled',
        )
        self.assertEqual(
            expected_disabled - actually_disabled,
            set(),
            'These widgets are unexpectedly enabled',
        )

    def check_widgets_enabled_for_markdown(self, window):
        self.check_widget_state(
            window,
            {'actionBold', 'actionItalic', 'actionUnderline', 'formattingBox', 'symbolBox'},
            set(),
        )

    def check_widgets_enabled_for_restructuredtext(self, window):
        self.check_widget_state(
            window,
            {'actionBold', 'actionItalic'},
            {'actionUnderline', 'formattingBox', 'symbolBox'},
        )

    def check_widgets_enabled(self, window, widgets):
        self.check_widget_state(window, set(widgets), set())

    def check_widgets_disabled(self, window, widgets):
        self.check_widget_state(window, set(), set(widgets))


    #
    # Tests
    #

    def test_windowTitleAndTabs_afterStartWithEmptyTab(self):
        self.window = ReTextWindow()
        self.window.createNew('')
        app.processEvents()

        self.assertEqual(1, self.window.tabWidget.count())
        self.assertEqual('New document[*]', self.window.windowTitle())
        self.assertFalse(self.window.currentTab.fileName)

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.md')], None),
    )
    def test_windowTitleAndTabs_afterLoadingFile(self, getOpenFileNamesMock):
        self.window = ReTextWindow()
        self.window.createNew('')
        self.window.actionOpen.trigger()
        app.processEvents()

        # Check that file is opened in the existing empty tab
        self.assertEqual(1, self.window.tabWidget.count())
        self.assertEqual('existing_file.md[*]', self.window.windowTitle())
        fileName = os.path.join('tests', 'testdata', 'existing_file.md')
        self.assertTrue(self.window.currentTab.fileName.endswith(fileName))
        self.assertEqual(self.window.tabWidget.tabText(0), 'existing_file')
        self.assertFalse(self.window.isWindowModified())

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.md')], None),
    )
    def test_windowTitleAndTabs_afterSwitchingTab(self, getOpenFileNamesMock):
        self.window = ReTextWindow()
        self.window.createNew('')
        self.window.actionOpen.trigger()
        app.processEvents()

        tab_with_file = self.window.currentTab

        self.window.createNew('bla')
        app.processEvents()

        tab_with_unsaved_content = self.window.currentTab

        self.assertEqual('New document[*]', self.window.windowTitle())
        self.assertIs(self.window.currentTab, tab_with_unsaved_content)
        self.assertIs(self.window.tabWidget.currentWidget(), tab_with_unsaved_content)
        self.assertEqual(self.window.ind, 1)
        self.assertEqual(self.window.tabWidget.tabText(0), 'existing_file')
        self.assertEqual(self.window.tabWidget.tabText(1), 'New document*')

        self.window.switchTab()
        app.processEvents()

        self.assertEqual('existing_file.md[*]', self.window.windowTitle())
        self.assertIs(self.window.currentTab, tab_with_file)
        self.assertIs(self.window.tabWidget.currentWidget(), tab_with_file)

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.md')], None),
    )
    def test_activeTab_afterLoadingFileThatIsAlreadyOpenInOtherTab(self, getOpenFileNamesMock):
        self.window = ReTextWindow()
        self.window.createNew('')
        self.window.actionOpen.trigger()
        app.processEvents()
        tab_with_file = self.window.currentTab

        self.window.createNew('')
        app.processEvents()

        # Make sure that the newly created tab is the active one
        self.assertFalse(self.window.currentTab.fileName)

        # Load the same document again
        self.window.actionOpen.trigger()
        app.processEvents()

        # Check that we have indeed been switched back to the previous tab
        self.assertIs(self.window.currentTab, tab_with_file)
        fileName = os.path.join('tests', 'testdata', 'existing_file.md')
        self.assertTrue(self.window.currentTab.fileName.endswith(fileName))

    def test_markupDependentWidgetStates_afterStartWithEmptyTabAndMarkdownAsDefaultMarkup(self):
        self.window = ReTextWindow()
        self.window.createNew('')
        app.processEvents()

        # markdown is the default markup
        self.check_widgets_enabled_for_markdown(self.window)

    def test_markupDependentWidgetStates_afterStartWithEmptyTabAndRestructuredtextAsDefaultMarkup(self):
        self.globalSettingsMock.defaultMarkup = 'reStructuredText'
        self.window = ReTextWindow()
        self.window.createNew('')
        app.processEvents()

        self.check_widgets_enabled_for_restructuredtext(self.window)

    def test_markupDependentWidgetStates_afterChangingDefaultMarkup(self):
        self.window = ReTextWindow()
        self.window.createNew('')
        app.processEvents()

        self.window.setDefaultMarkup(markups.ReStructuredTextMarkup)

        self.check_widgets_enabled_for_restructuredtext(self.window)

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.md')], None),
    )
    def test_markupDependentWidgetStates_afterLoadingMarkdownDocument(self, getOpenFileNamesMock):
        self.window = ReTextWindow()
        self.window.createNew('')
        self.window.actionOpen.trigger()
        app.processEvents()

        self.check_widgets_enabled_for_markdown(self.window)

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.rst')], None),
    )
    def test_markupDependentWidgetStates_afterLoadingRestructuredtextDocument(
        self,
        getOpenFileNamesMock,
    ):
        self.window = ReTextWindow()
        self.window.createNew('')
        self.window.actionOpen.trigger()
        app.processEvents()

        self.check_widgets_enabled_for_restructuredtext(self.window)

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        side_effect=[
            ([os.path.join(path_to_testdata, 'existing_file.md')], None),
            ([os.path.join(path_to_testdata, 'existing_file.rst')], None),
        ],
    )
    def test_markupDependentWidgetStates_afterSwitchingTab(self, getOpenFileNamesMock):
        self.window = ReTextWindow()
        self.window.createNew('')
        self.window.actionOpen.trigger()
        self.window.actionOpen.trigger()
        app.processEvents()

        # Just to make sure that sending two actionOpen triggers has had the desired effect
        self.assertIn('.rst', self.window.windowTitle())

        self.window.switchTab()
        app.processEvents()

        self.assertIn('.md', self.window.windowTitle())
        self.check_widgets_enabled_for_markdown(self.window)

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.md')], None),
    )
    @patch(
        'ReText.window.QFileDialog.getSaveFileName',
        return_value=(os.path.join(path_to_testdata, 'not_existing_file.rst'), None),
    )
    def test_markupDependentWidgetStates_afterSavingDocumentAsDifferentMarkup(
        self,
        getSaveFileNameMock,
        getOpenFileNamesMock,
    ):
        self.window = ReTextWindow()
        self.window.createNew('')
        self.window.actionOpen.trigger()
        app.processEvents()

        try:
            self.window.actionSaveAs.trigger()
            app.processEvents()

        finally:
            os.remove(os.path.join(path_to_testdata, 'not_existing_file.rst'))

        self.check_widgets_enabled_for_restructuredtext(self.window)

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.md')], None),
    )
    @patch(
        'ReText.window.QFileDialog.getSaveFileName',
        return_value=(os.path.join(path_to_testdata, 'not_existing_file.md'), None),
    )
    def test_saveWidgetStates(self, getSaveFileNameMock, getOpenFileNamesMock):
        self.window = ReTextWindow()

        # check if save is disabled at first
        self.window.createNew('')
        app.processEvents()
        self.check_widgets_disabled(self.window, ('actionSave',))
        self.assertFalse(self.window.isWindowModified())
        self.assertEqual(self.window.tabWidget.tabText(0), 'New document')

        # check if it's enabled after inserting some text
        self.window.currentTab.editBox.textCursor().insertText('some text')
        app.processEvents()
        self.check_widgets_enabled(self.window, ('actionSave',))
        self.assertTrue(self.window.isWindowModified())
        self.assertEqual(self.window.tabWidget.tabText(0), 'New document*')

        # check if it's disabled again after loading a file in a second tab and switching to it
        self.window.actionOpen.trigger()
        app.processEvents()
        self.check_widgets_disabled(self.window, ('actionSave',))
        self.assertFalse(self.window.isWindowModified())
        self.assertEqual(self.window.tabWidget.tabText(0), 'New document*')
        self.assertEqual(self.window.tabWidget.tabText(1), 'existing_file')

        # check if it's enabled again after switching back
        self.window.switchTab()
        app.processEvents()
        self.check_widgets_enabled(self.window, ('actionSave',))
        self.assertTrue(self.window.isWindowModified())
        self.assertEqual(self.window.tabWidget.tabText(0), 'New document*')
        self.assertEqual(self.window.tabWidget.tabText(1), 'existing_file')

        # check if it's disabled after saving
        try:
            self.window.actionSaveAs.trigger()
            app.processEvents()
            self.check_widgets_disabled(self.window, ('actionSave',))
            self.assertFalse(self.window.isWindowModified())
            self.assertEqual(self.window.tabWidget.tabText(0), 'not_existing_file')
            self.assertEqual(self.window.tabWidget.tabText(1), 'existing_file')
        finally:
            os.remove(os.path.join(path_to_testdata, 'not_existing_file.md'))

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.md')], None),
    )
    def test_encodingAndReloadWidgetStates(self, getOpenFileNamesMock):
        self.window = ReTextWindow()

        # check if reload/set encoding is disabled for a tab without filename set
        self.window.createNew('')
        app.processEvents()
        self.check_widgets_disabled(self.window, ('actionReload','actionSetEncoding'))

        self.window.actionOpen.trigger()
        app.processEvents()
        self.check_widgets_enabled(self.window, ('actionReload','actionSetEncoding'))

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.md')], None),
    )
    def test_encodingAndReloadWidgetStates_alwaysDisabledWhenAutosaveEnabled(self, getOpenFileNamesMock):
        self.globalSettingsMock.autoSave = True
        self.window = ReTextWindow()

        # check if reload/set encoding is disabled for a tab without filename set
        self.window.createNew('')
        app.processEvents()
        self.check_widgets_disabled(self.window, ('actionReload','actionSetEncoding'))

        self.window.actionOpen.trigger()
        app.processEvents()
        self.check_widgets_disabled(self.window, ('actionReload','actionSetEncoding'))

    @patch(
        'ReText.window.QFileDialog.getOpenFileNames',
        return_value=([os.path.join(path_to_testdata, 'existing_file.md')], None),
    )
    def test_copyFilePathCopiesActiveDocumentPath(self, getOpenFileNamesMock):
        self.window = ReTextWindow()
        self.window.createNew('')
        app.processEvents()
        self.assertFalse(self.window.actionCopyFilePath.isEnabled())

        self.window.actionOpen.trigger()
        app.processEvents()
        self.assertTrue(self.window.actionCopyFilePath.isEnabled())

        self.window.actionCopyFilePath.trigger()
        self.assertEqual(app.clipboard().text(), self.window.currentTab.fileName)

    def test_doesNotTweakSpecialCharacters(self):
        fileName = tempfile.mkstemp(suffix='.mkd')[1]
        content = 'Non-breaking\u00a0space\n\nLine\u2028separator\n'
        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write(content)
        window = ReTextWindow()
        window.openFileWrapper(fileName)
        self.assertTrue(window.saveFile())
        with open(fileName, encoding='utf-8') as tempFile:
            self.assertMultiLineEqual(content, tempFile.read())
        with suppress(PermissionError):
            os.remove(fileName)

    def test_autoSave(self):
        self.globalSettingsMock.autoSave = True
        window = ReTextWindow()
        window.autoSaveTimer.start(250)
        fileName = tempfile.mkstemp(suffix='.mkd')[1]
        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write('first content')
        window.openFileWrapper(fileName)

        cursor = window.currentTab.editBox.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText('second content')
        QTest.qWait(300)  # more than the timer interval
        with open(fileName, encoding='utf-8') as tempFile:
            self.assertEqual(tempFile.read(), 'second content')

        window.closeTab(0)
        with suppress(PermissionError):
            os.remove(fileName)

    @unittest.skipIf(platform.system() == 'Windows', 'QFileSystemWatcher does not work reliably')
    @patch('ReText.window.QMessageBox.exec', return_value=None)
    def test_reloadFileNotModified(self, messageBoxExecMock):
        self.fileSystemWatcherPatcher.stop()
        window = ReTextWindow()
        fileName = tempfile.mkstemp(suffix='.mkd')[1]
        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write('first content')
        window.openFileWrapper(fileName)
        self.assertEqual(window.fileSystemWatcher.files(), [fileName.replace('\\', '/')])
        editBox = window.currentTab.editBox
        self.assertEqual(editBox.toPlainText(), 'first content')
        self.assertFalse(editBox.document().isModified())
        app.processEvents()

        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write('modified externally')
        QTest.qWait(100)
        self.assertEqual(editBox.toPlainText(), 'modified externally')
        self.assertFalse(window.currentTab.forceDisableAutoSave)

        window.closeTab(0)
        self.assertEqual(window.fileSystemWatcher.files(), [])
        with suppress(PermissionError):
            os.remove(fileName)

    @unittest.skipIf(platform.system() == 'Windows', 'QFileSystemWatcher does not work reliably')
    @patch('ReText.window.QMessageBox.warning', return_value=QMessageBox.StandardButton.Discard)
    @patch('ReText.window.QMessageBox.exec', return_value=None)
    def test_reloadFileModified(self, messageBoxExecMock, messageBoxWarningMock):
        self.fileSystemWatcherPatcher.stop()
        window = ReTextWindow()
        fileName = tempfile.mkstemp(suffix='.mkd')[1]
        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write('first content')
        window.openFileWrapper(fileName)
        self.assertEqual(window.fileSystemWatcher.files(), [fileName.replace('\\', '/')])
        editBox = window.currentTab.editBox
        self.assertEqual(editBox.toPlainText(), 'first content')

        editBox.textCursor().insertText('modified ')
        app.processEvents()
        self.assertTrue(editBox.document().isModified())

        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write('modified externally')
        QTest.qWait(100)
        self.assertEqual(editBox.toPlainText(), 'modified first content')
        self.assertTrue(window.currentTab.forceDisableAutoSave)

        window.closeTab(0)
        self.assertEqual(window.fileSystemWatcher.files(), [])
        with suppress(PermissionError):
            os.remove(fileName)

    @unittest.skipIf(platform.system() == 'Windows', 'QFileSystemWatcher does not work reliably')
    @patch('ReText.window.QMessageBox.exec', return_value=None)
    def test_reloadFileReplacedAtomically(self, messageBoxExecMock):
        # Many applications do not save files in place: they write a new
        # file and rename it over the original. That replaces the file
        # the watch refers to, so it has to be re-created every time.
        # See https://github.com/retext-project/retext/issues/137
        self.fileSystemWatcherPatcher.stop()
        window = ReTextWindow()
        handle, fileName = tempfile.mkstemp(suffix='.mkd')
        os.close(handle)  # the file has to be replaceable and removable
        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write('first content')
        window.openFileWrapper(fileName)
        self.assertEqual(window.fileSystemWatcher.files(), [fileName.replace('\\', '/')])
        editBox = window.currentTab.editBox
        self.assertEqual(editBox.toPlainText(), 'first content')
        app.processEvents()

        def replaceAtomically(content):
            tmpName = fileName + '.tmp'
            with open(tmpName, 'w', encoding='utf-8') as tempFile:
                tempFile.write(content)
            os.replace(tmpName, fileName)

        replaceAtomically('modified externally once')
        QTest.qWait(100)
        self.assertEqual(editBox.toPlainText(), 'modified externally once')
        self.assertEqual(window.fileSystemWatcher.files(), [fileName.replace('\\', '/')])

        # A second replacement must be detected as well, which means the
        # watch was re-established after the first one.
        replaceAtomically('modified externally twice')
        QTest.qWait(100)
        self.assertEqual(editBox.toPlainText(), 'modified externally twice')
        self.assertEqual(window.fileSystemWatcher.files(), [fileName.replace('\\', '/')])

        window.closeTab(0)
        self.assertEqual(window.fileSystemWatcher.files(), [])
        with suppress(PermissionError):
            os.remove(fileName)

    @unittest.skipIf(platform.system() == 'Windows', 'QFileSystemWatcher does not work reliably')
    @patch('ReText.window.QMessageBox.warning', return_value=QMessageBox.StandardButton.Discard)
    @patch('ReText.window.QMessageBox.exec', return_value=None)
    def test_rewatchFileAfterItReappears(self, messageBoxExecMock, messageBoxWarningMock):
        # Some saves remove the file before writing the new one, so it is
        # briefly absent. The watch cannot be re-created while that is the
        # case, and giving up then would leave the file unwatched forever.
        self.fileSystemWatcherPatcher.stop()
        window = ReTextWindow()
        handle, fileName = tempfile.mkstemp(suffix='.mkd')
        os.close(handle)  # the file has to be replaceable and removable
        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write('first content')
        window.openFileWrapper(fileName)
        self.assertEqual(window.fileSystemWatcher.files(), [fileName.replace('\\', '/')])
        editBox = window.currentTab.editBox
        self.assertEqual(editBox.toPlainText(), 'first content')
        app.processEvents()

        os.remove(fileName)
        QTest.qWait(100)  # let the watcher notice that the file is gone
        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write('recreated externally')
        QTest.qWait(MAX_REWATCH_ATTEMPTS * REWATCH_RETRY_INTERVAL)
        self.assertEqual(window.fileSystemWatcher.files(), [fileName.replace('\\', '/')])

        # The watch has to be functional again, not merely registered.
        editBox.document().setModified(False)
        with open(fileName, 'w', encoding='utf-8') as tempFile:
            tempFile.write('modified after recreation')
        QTest.qWait(100)
        self.assertEqual(editBox.toPlainText(), 'modified after recreation')

        window.closeTab(0)
        self.assertEqual(window.fileSystemWatcher.files(), [])
        with suppress(PermissionError):
            os.remove(fileName)

    def test_savePreviewState(self):
        self.globalSettingsMock.openLastFilesOnStartup = True
        self.globalSettingsMock.savePreviewState = True
        fileName = os.path.join(path_to_testdata, 'existing_file.md')

        window = ReTextWindow()
        window.openFileWrapper(fileName)
        window.setPreviewState(PreviewLive)
        self.assertEqual(window.currentTab.previewState, PreviewLive)
        window.close()
        self.assertEqual(self.globalCacheMock.lastFileList, [fileName])
        self.assertEqual(self.globalCacheMock.lastPreviewStateList, ['live-preview'])

        # Reopening the document has to bring the live preview back.
        restoredWindow = ReTextWindow()
        restoredWindow.restoreLastOpenedFiles()
        self.assertEqual(restoredWindow.currentTab.fileName, fileName)
        self.assertEqual(restoredWindow.currentTab.previewState, PreviewLive)

    def test_savePreviewStateOverridesDefaultPreviewState(self):
        # A document closed in the editor stays in the editor, even when
        # defaultPreviewState asks for a preview.
        self.globalSettingsMock.openLastFilesOnStartup = True
        self.globalSettingsMock.savePreviewState = True
        self.globalSettingsMock.defaultPreviewState = 'live-preview'
        self.globalCacheMock.lastFileList = [os.path.join(path_to_testdata, 'existing_file.md')]
        self.globalCacheMock.lastPreviewStateList = ['editor']

        window = ReTextWindow()
        window.restoreLastOpenedFiles()
        self.assertEqual(window.currentTab.previewState, PreviewDisabled)

    def test_anchorExtensionIsRequested(self):
        # Links inside a document point at identifiers that Markdown only
        # gives its headings when the toc extension is asked for.
        window = ReTextWindow()
        window.openFileWrapper(os.path.join(path_to_testdata, 'existing_file.md'))
        tab = window.currentTab
        tab.converterProcess = MagicMock()
        tab.startPendingConversion()
        self.assertIn('toc', tab.converterProcess.start_conversion.call_args[0][2])
        window.closeTab(0)

    def test_savePreviewStateDisabled(self):
        self.globalSettingsMock.openLastFilesOnStartup = True
        self.globalSettingsMock.savePreviewState = False
        self.globalSettingsMock.defaultPreviewState = 'normal-preview'
        fileName = os.path.join(path_to_testdata, 'existing_file.md')

        window = ReTextWindow()
        window.openFileWrapper(fileName)
        self.assertEqual(window.currentTab.previewState, PreviewNormal)
        window.setPreviewState(PreviewDisabled)
        window.close()
        self.assertEqual(self.globalCacheMock.lastPreviewStateList, [])

        # With the option off, defaultPreviewState decides, as before.
        self.globalCacheMock.lastPreviewStateList = ['editor']
        restoredWindow = ReTextWindow()
        restoredWindow.restoreLastOpenedFiles()
        self.assertEqual(restoredWindow.currentTab.previewState, PreviewNormal)

if __name__ == '__main__':
    unittest.main()
