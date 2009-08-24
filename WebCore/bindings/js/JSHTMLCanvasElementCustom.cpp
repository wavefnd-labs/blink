/*
 * Copyright (C) 2007 Apple Inc. All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY APPLE COMPUTER, INC. ``AS IS'' AND ANY
 * EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
 * PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL APPLE COMPUTER, INC. OR
 * CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
 * PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
 * PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
 * OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE. 
 */

#include "config.h"
#include "JSHTMLCanvasElement.h"

#include "HTMLCanvasElement.h"
#include "JSCanvasRenderingContext2D.h"
#if ENABLE(3D_CANVAS)
#include "JSCanvasRenderingContext3D.h"
#include <wtf/GetPtr.h>
#endif

using namespace JSC;

namespace WebCore {

#if ENABLE(3D_CANVAS)
JSValue JSHTMLCanvasElement::getContext(JSC::ExecState* exec, JSC::ArgList const& args)
{
    HTMLCanvasElement* imp = static_cast<HTMLCanvasElement*>(impl());
    const UString& contextId = args.at(0).toString(exec);

    if (contextId == "2d")
        return toJS(exec, WTF::getPtr(reinterpret_cast<CanvasRenderingContext2D*>(imp->getContext(contextId))));
    if (contextId == "webkit-3d")
        return toJS(exec, WTF::getPtr(reinterpret_cast<CanvasRenderingContext3D*>(imp->getContext(contextId))));
    
    return jsUndefined();
}
#endif

void JSHTMLCanvasElement::markChildren(MarkStack& markStack)
{
    Base::markChildren(markStack);

    HTMLCanvasElement* canvas = static_cast<HTMLCanvasElement*>(impl());
    JSGlobalData& globalData = *Heap::heap(this)->globalData();

    markDOMObjectWrapper(markStack, globalData, canvas->renderingContext2D());
}

} // namespace WebCore
