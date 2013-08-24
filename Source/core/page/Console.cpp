/*
 * Copyright (C) 2007 Apple Inc. All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 *
 * 1.  Redistributions of source code must retain the above copyright
 *     notice, this list of conditions and the following disclaimer.
 * 2.  Redistributions in binary form must reproduce the above copyright
 *     notice, this list of conditions and the following disclaimer in the
 *     documentation and/or other materials provided with the distribution.
 * 3.  Neither the name of Apple Computer, Inc. ("Apple") nor the names of
 *     its contributors may be used to endorse or promote products derived
 *     from this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY APPLE AND ITS CONTRIBUTORS "AS IS" AND ANY
 * EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL APPLE OR ITS CONTRIBUTORS BE LIABLE FOR ANY
 * DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
 * (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 * LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
 * ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
 * THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "config.h"
#include "core/page/Console.h"

#include "bindings/v8/ScriptCallStackFactory.h"
#include "bindings/v8/ScriptProfiler.h"
#include "core/inspector/ConsoleAPITypes.h"
#include "core/inspector/InspectorConsoleInstrumentation.h"
#include "core/inspector/ScriptArguments.h"
#include "core/inspector/ScriptCallStack.h"
#include "core/inspector/ScriptProfile.h"
#include "core/page/Chrome.h"
#include "core/page/ChromeClient.h"
#include "core/page/ConsoleBase.h"
#include "core/page/ConsoleTypes.h"
#include "core/page/Frame.h"
#include "core/page/MemoryInfo.h"
#include "core/page/Page.h"
#include "wtf/text/CString.h"
#include "wtf/text/WTFString.h"

#include "core/platform/chromium/TraceEvent.h"

namespace WebCore {

Console::Console(Frame* frame)
    : DOMWindowProperty(frame)
{
    ScriptWrappable::init(this);
}

Console::~Console()
{
}

ScriptExecutionContext* Console::context()
{
    if (!m_frame)
        return 0;
    return m_frame->document();
}

void Console::internalAddMessage(MessageType type, MessageLevel level, ScriptState* state, PassRefPtr<ScriptArguments> scriptArguments, bool acceptNoArguments, bool printTrace)
{
    RefPtr<ScriptArguments> arguments = scriptArguments;

    if (!m_frame)
        return;

    if (!acceptNoArguments && !arguments->argumentCount())
        return;


    String message;
    bool gotMessage = arguments->getFirstArgumentAsString(message);

    InspectorInstrumentation::addMessageToConsole(context(), ConsoleAPIMessageSource, type, level, message, state, arguments);

    String stackTrace;
    if (gotMessage) {
        RefPtr<ScriptCallStack> callStack(createScriptCallStack(state, 1));
        if (m_frame->page()->chrome().client().shouldReportDetailedMessageForSource(callStack->at(0).sourceURL())) {
            callStack = createScriptCallStack(ScriptCallStack::maxCallStackSizeToCapture);
            stackTrace = ConsoleBase::formatStackTraceString(message, callStack);
        }
        m_frame->page()->chrome().client().addMessageToConsole(ConsoleAPIMessageSource, level, message, callStack->at(0).lineNumber(), callStack->at(0).sourceURL(), stackTrace);
    }

}


bool Console::profilerEnabled()
{
    return InspectorInstrumentation::profilerEnabled(m_frame->page());
}

PassRefPtr<MemoryInfo> Console::memory() const
{
    // FIXME: Because we create a new object here each time,
    // console.memory !== console.memory, which seems wrong.
    return MemoryInfo::create(m_frame);
}

} // namespace WebCore
