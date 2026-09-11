# vim: ts=4:sw=4:expandtab

# This file is part of ReText
# Copyright: 2017-2025 Dmitry Shachnev
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

import json
import os
from contextlib import suppress
from tempfile import mkstemp

from PyQt6.QtCore import QEvent, Qt, QUrl
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QFontInfo,
    QGuiApplication,
    QPalette,
    QTextDocument,
)
from PyQt6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineSettings,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QLabel

from ReText import globalCache, globalSettings
from ReText.editor import getColor
from ReText.syncscroll import SyncScroll

# setHtml() hands the content to Chromium through a data: URL, and Chromium
# refuses URLs longer than 2 MB (url::kMaxURLChars). Base64 inflates the
# content by a third on the way into the URL, and the failure is silent:
# nothing is loaded, and nothing is reported. Content above this size is
# loaded from a file of its own instead.
MAX_DATA_URL_CONTENT_SIZE = 1024 * 1024


def removeFile(fileName):
    with suppress(OSError):
        os.remove(fileName)


def samePath(fileName, otherFileName):
    '''
    Tell whether two names refer to the same file.

    QUrl.toLocalFile() returns a path with forward slashes, while the names
    ReText keeps come from the file system, which on Windows means back
    slashes and no significant case, so the two cannot be compared as they
    are.
    '''
    if fileName is None or otherFileName is None:
        return False
    return (os.path.normcase(os.path.normpath(fileName))
            == os.path.normcase(os.path.normpath(otherFileName)))


class ReTextWebEngineUrlRequestInterceptor(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        if (info.resourceType() == QWebEngineUrlRequestInfo.ResourceType.ResourceTypeXhr
                and info.requestUrl().isLocalFile()):
            # For security reasons, disable XMLHttpRequests to local files
            info.block(True)


def str_rgba(color: QColor):
    """ Todo: More elegant use of QColor with alpha in stylesheet """
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"

class UrlPopup(QLabel):
    def __init__(self, window):
        super().__init__(window)
        self.window = window

        self.setStyleSheet('''
            border: 1px solid {:s};
            border-radius: 3px;
            background: {:s};
            '''.format(str_rgba(getColor('urlPopupBorder')),
                       str_rgba(getColor('urlPopupArea'))))
        self.fontHeight = self.fontMetrics().height()
        self.setVisible(False)

    def pop(self, url: str):
        """ Show link target on mouse hover

        QWebEnginePage emits signal 'linkHovered' which provides
        url: str -- target of link hovered (or empty on mouse-release)
        """
        if url:
            self.setText(url)
            windowBottom = self.window.rect().bottom()
            textWidth = self.fontMetrics().horizontalAdvance(url)
            self.setGeometry(-2, windowBottom-self.fontHeight-6,
                    textWidth+10, self.fontHeight+10)
            self.setVisible(True)
        else:
            self.setVisible(False)


class ReTextWebEnginePage(QWebEnginePage):
    def __init__(self, parent, tab):
        QWebEnginePage.__init__(self, parent)
        self.preview = parent
        self.tab = tab
        self.interceptor = ReTextWebEngineUrlRequestInterceptor(self)
        self.setUrlRequestInterceptor(self.interceptor)
        self.urlPopup = UrlPopup(self.tab.p)
        self.linkHovered.connect(self.urlPopup.pop)

    def setScrollPosition(self, pos):
        self.runJavaScript(f"window.scrollTo({pos.x()}, {pos.y()});")

    def getPositionMap(self, callback):
        def resultCallback(result):
            if result:
                return callback({int(a): b for a, b in result.items()})

        script = """
        var elements = document.querySelectorAll('[data-posmap]');
        var result = {};
        var bodyTop = document.body.getBoundingClientRect().top;
        for (var i = 0; i < elements.length; ++i) {
            var element = elements[i];
            value = element.getAttribute('data-posmap');
            bottom = element.getBoundingClientRect().bottom - bodyTop;
            result[value] = bottom;
        }
        result;
        """
        self.runJavaScript(script, resultCallback)

    def scrollToAnchor(self, anchor):
        anchorLiteral = json.dumps(anchor)
        script = f"""
        var anchor = {anchorLiteral};
        var element = document.getElementById(anchor) ||
                      document.getElementsByName(anchor)[0];
        if (element) {{
            element.scrollIntoView();
        }}
        """
        self.runJavaScript(script)

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceId):
        print(f"level={level!r} message={message!r} lineNumber={lineNumber!r} sourceId={sourceId!r}")

    def acceptNavigationRequest(self, url, type, isMainFrame):
        if not isMainFrame:
            return True
        if url.scheme() == "data":
            return True
        if url.isLocalFile():
            localFile = url.toLocalFile()
            if samePath(localFile, self.preview.contentFileName):
                # Our own copy of the content of the preview, which is only
                # used for content that is too large for a data: URL, see
                # ReTextWebEnginePreview.setHtml().
                return True
            if samePath(localFile, self.tab.fileName):
                if url.hasFragment():
                    # A link to a place inside the document. The preview is
                    # not necessarily loaded from the document itself, and
                    # then this is not a navigation within the same page,
                    # which is what it is meant to be: scroll to the anchor
                    # rather than let anything be loaded for it.
                    self.scrollToAnchor(url.fragment())
                    return False
                self.tab.startPendingConversion()
                return False
            if self.tab.openSourceFile(localFile):
                return False
        if globalSettings.handleWebLinks:
            return True
        QDesktopServices.openUrl(url)
        return False


class ReTextWebEnginePreview(QWebEngineView):

    def __init__(self, tab,
                 editorPositionToSourceLineFunc,
                 sourceLineToEditorPositionFunc):

        QWebEngineView.__init__(self, parent=tab)
        self.contentFileName = None
        webPage = ReTextWebEnginePage(self, tab)

        handCursor = QCursor(Qt.CursorShape.PointingHandCursor)
        arrowCursor = QCursor(Qt.CursorShape.ArrowCursor)
        webPage.linkHovered.connect(lambda value: self.setCursor(handCursor if value else arrowCursor))

        self.setPage(webPage)

        self.editBox = tab.editBox
        self.syncscroll = SyncScroll(
            webPage,
            editorPositionToSourceLineFunc,
            sourceLineToEditorPositionFunc,
            setEditorScrollValueFunc=self.editBox.verticalScrollBar().setValue,
            isPreviewVisibleFunc=self.isVisible,
        )

        settings = self.settings()
        settings.setDefaultTextEncoding('utf-8')
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        if hasattr(QWebEngineSettings.WebAttribute, 'ForceDarkMode'):  # Qt >= 6.7
            # QGuiApplication.instance().styleHints().colorScheme() does not seem to
            # work properly on KDE Plasma, so let's re-use the same approach as our
            # editor uses.
            palette = QApplication.palette()
            windowColor = palette.color(QPalette.ColorRole.Window)
            if windowColor.lightness() <= 150:
                settings.setAttribute(QWebEngineSettings.WebAttribute.ForceDarkMode, True)

        # Events relevant to sync scrolling
        self.editBox.cursorPositionChanged.connect(self._handleCursorPositionChanged)
        self.editBox.verticalScrollBar().valueChanged.connect(self.syncscroll.handleEditorScrolled)
        self.editBox.resized.connect(self._handleEditorResized)

        # When preview is scrolled, update the editor scroll accordingly
        webPage.scrollPositionChanged.connect(self.syncscroll.handlePreviewScrolled)

        # Scroll the preview when the mouse wheel is used to scroll
        # beyond the beginning/end of the editor
        self.editBox.scrollLimitReached.connect(self._handleWheelEvent)

    def setFont(self, font):
        settings = self.settings()
        settings.setFontFamily(QWebEngineSettings.FontFamily.StandardFont,
                               font.family())
        pixelSize = QFontInfo(font).pixelSize()
        settings.setFontSize(QWebEngineSettings.FontSize.DefaultFontSize, pixelSize)
        settings.setFontSize(QWebEngineSettings.FontSize.DefaultFixedFontSize, pixelSize)

    def setHtml(self, html, baseUrl):
        # A hack to prevent WebEngine from stealing the focus
        self.setEnabled(False)
        content = html.encode('utf-8')
        if len(content) > MAX_DATA_URL_CONTENT_SIZE:
            self.load(QUrl.fromLocalFile(self.writeContentToFile(content)))
        else:
            super().setHtml(html, baseUrl)
        self.setEnabled(True)

    def writeContentToFile(self, content):
        '''
        Write the content of the preview to a file of its own and return its
        name. The same file is reused for every later update.

        Relative links and images are resolved against the base element that
        the preview carries, see ReTextTab.getHtmlFromConverted(), so they
        keep working even though the document is loaded from somewhere else.
        '''
        if self.contentFileName is None:
            handle, fileName = mkstemp(prefix='retext-preview-', suffix='.html')
            os.close(handle)
            self.contentFileName = fileName
            # The file belongs to this preview, so it goes away with it.
            # fileName rather than self is captured on purpose, to not keep
            # the preview alive through its own signal.
            self.destroyed.connect(lambda: removeFile(fileName))
        with open(self.contentFileName, 'wb') as contentFile:
            contentFile.write(content)
        return self.contentFileName

    def _handleWheelEvent(self, event):
        # Only pass wheelEvents on to the preview if syncscroll is
        # controlling the position of the preview
        if self.syncscroll.isActive():
            QGuiApplication.sendEvent(self.focusProxy(), event)

    def event(self, event):
        # Work-around https://bugreports.qt.io/browse/QTBUG-43602
        if event.type() == QEvent.Type.ChildAdded:
            event.child().installEventFilter(self)
        elif event.type() == QEvent.Type.ChildRemoved:
            event.child().removeEventFilter(self)
        return super().event(event)

    def eventFilter(self, object, event):
        if event.type() == QEvent.Type.Wheel:
            if QGuiApplication.keyboardModifiers() == Qt.KeyboardModifier.ControlModifier:
                self.wheelEvent(event)
                return True
        return False

    def findText(self, text, flags):
        options = QWebEnginePage.FindFlag(0)
        if flags & QTextDocument.FindFlag.FindBackward:
            options |= QWebEnginePage.FindFlag.FindBackward
        if flags & QTextDocument.FindFlag.FindCaseSensitively:
            options |= QWebEnginePage.FindFlag.FindCaseSensitively
        super().findText(text, options)
        return True

    def disconnectExternalSignals(self):
        self.editBox.cursorPositionChanged.disconnect(self._handleCursorPositionChanged)
        self.editBox.verticalScrollBar().valueChanged.disconnect(self.syncscroll.handleEditorScrolled)
        self.editBox.resized.disconnect(self._handleEditorResized)

        self.editBox.scrollLimitReached.disconnect(self._handleWheelEvent)
        # Disconnect preview scroll synchronization
        self.page().scrollPositionChanged.disconnect(self.syncscroll.handlePreviewScrolled)

    def _handleCursorPositionChanged(self):
        editorCursorPosition = self.editBox.verticalScrollBar().value() + \
                       self.editBox.cursorRect().top()
        self.syncscroll.handleCursorPositionChanged(editorCursorPosition)

    def _handleEditorResized(self, rect):
        self.syncscroll.handleEditorResized(rect.height())

    def wheelEvent(self, event):
        if QGuiApplication.keyboardModifiers() == Qt.KeyboardModifier.ControlModifier:
            newZoomFactor = globalCache.webEngineZoomFactor * (1.001 ** event.angleDelta().y())
            # Valid values are within the range from 0.25 to 5.0.
            globalCache.webEngineZoomFactor = max(min(newZoomFactor, 5.0), 0.25)
            self.setZoomFactor(globalCache.webEngineZoomFactor)
        return super().wheelEvent(event)

    def showEvent(self, event):
        self.setZoomFactor(globalCache.webEngineZoomFactor)
        self.syncscroll.handlePreviewShown()
        return super().showEvent(event)
