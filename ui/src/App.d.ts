declare global {
    interface Window {
        MathJax?: {
            typesetPromise?: (elements?: HTMLElement[]) => Promise<void>;
            startup?: {
                typeset?: boolean;
            };
            svg?: {
                fontCache?: string;
            };
        };
    }
}
export declare function App(): import("react").JSX.Element;
